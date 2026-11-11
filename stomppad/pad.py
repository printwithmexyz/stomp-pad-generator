"""SVG in, pad design out: the one pipeline the desktop and web apps both call."""

from dataclasses import dataclass, field, replace

import numpy as np
from shapely import affinity
from shapely.geometry import Polygon

from .packing import PackingSettings, pack_regions
from .refine import CleanupSettings, clean_regions, printability_warnings, scale_regions_to_mm
from .svg_parse import parse_svg, polygon_parts, regions_from_fills, robust_union

VOID_MODES = ('through', 'recess')


@dataclass(frozen=True)
class PadSettings:
    target_width: float = 100.0
    target_height: float = None
    samples_per_segment: int = 20
    keep_background: bool = False
    void_fills: tuple = ()
    multicolor: bool = False  # False merges every fill into one color before cleanup and packing

    base_thickness: float = 2.0
    outline_offset: float = 2.0
    outline_height: float = 0.8
    pyramid_height: float = 2.5
    pyramid_style: int = 4

    # 'through' cuts enclosed holes through the pad. 'recess' leaves a floor
    # recess_depth below the surface the pyramids stand on.
    void_mode: str = 'through'
    recess_depth: float = 0.6

    packing: PackingSettings = PackingSettings()
    cleanup: CleanupSettings = CleanupSettings()

    def __post_init__(self):
        if self.void_mode not in VOID_MODES:
            raise ValueError(f'void_mode must be one of {VOID_MODES}, got {self.void_mode!r}')
        if self.recess_depth < 0:
            raise ValueError('recess_depth cannot be negative')


@dataclass
class PadDesign:
    settings: PadSettings
    regions: list   # millimetres, Y up, artwork's lower-left corner at the origin
    art: object     # floor the pyramids stand on: every region, hairline gaps closed
    voids: object   # enclosed holes in the art
    packed: dict    # region id -> PackedRegion
    warnings: list = field(default_factory=list)

    @property
    def width(self):
        return self.art.bounds[2] - self.art.bounds[0]

    @property
    def height(self):
        return self.art.bounds[3] - self.art.bounds[1]

    @property
    def voids_are_recessed(self):
        return (self.settings.void_mode == 'recess'
                and self.settings.recess_depth < self.settings.base_thickness)

    def positions(self):
        return np.vstack([packed.positions for packed in self.packed.values()])


def _log(logger, message):
    (logger or print)(message)


def _as_one_color(regions):
    return regions_from_fills({regions[0].fill: robust_union([r.geometry for r in regions])})


def _fit_to_size(regions, target_width, target_height):
    """Cleanup can trim the extremes, so stretch back to the size that was asked for."""
    min_x, min_y, max_x, max_y = robust_union([r.geometry for r in regions]).bounds
    scale = target_width / (max_x - min_x)
    if target_height:
        scale = min(scale, target_height / (max_y - min_y))
    matrix = [scale, 0, 0, scale, -min_x * scale, -min_y * scale]
    return [
        replace(region, geometry=affinity.affine_transform(region.geometry, matrix))
        for region in regions
    ]


def _close_gaps(geometry, radius):
    """Bridge gaps narrower than 2 * radius so neighbouring colors share one floor."""
    grown = geometry.buffer(radius, join_style='mitre')
    return grown.buffer(-radius, join_style='mitre')


def _enclosed_holes(art):
    outlines = robust_union([Polygon(polygon.exterior) for polygon in polygon_parts(art)])
    return outlines.difference(art)


def build_pad(svg_file, settings=PadSettings(), logger=None):
    """Parse, scale, clean and pack an SVG. Raises ValueError if nothing printable is left."""
    parsed = parse_svg(
        svg_file,
        samples_per_segment=settings.samples_per_segment,
        keep_background=settings.keep_background,
        void_fills=settings.void_fills,
    )
    warnings = list(parsed.warnings)
    if not parsed.regions:
        raise ValueError(' '.join(warnings))

    regions, _ = scale_regions_to_mm(parsed.regions, settings.target_width, settings.target_height)
    if not settings.multicolor:
        regions = _as_one_color(regions)
    regions, notes = clean_regions(regions, settings.cleanup)
    warnings += notes
    if not regions:
        raise ValueError('Every shape was too small to print at this size. ' + ' '.join(warnings))
    regions = _fit_to_size(regions, settings.target_width, settings.target_height)

    art = _close_gaps(robust_union([r.geometry for r in regions]), settings.cleanup.min_feature / 2)
    voids = _enclosed_holes(art)
    packed = pack_regions(regions, settings.packing)

    warnings += [warning.message for warning in printability_warnings(
        regions, settings.packing.pyramid_size, settings.packing.safety_margin,
    )]
    if settings.void_mode == 'recess' and settings.recess_depth >= settings.base_thickness:
        warnings.append('Recess depth is at least the base thickness, so holes are cut through.')

    design = PadDesign(settings, regions, art, voids, packed, warnings)
    pyramid_count = len(design.positions())
    _log(logger, f'  {len(regions)} region(s) in {len({r.fill for r in regions})} color(s), '
                 f'{design.width:.1f} x {design.height:.1f} mm, {pyramid_count} pyramids')
    for warning in warnings:
        _log(logger, f'  NOTE: {warning}')
    return design
