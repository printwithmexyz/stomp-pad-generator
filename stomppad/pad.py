"""SVG in, pad design out: the one pipeline the desktop and web apps both call."""

from dataclasses import dataclass, field, fields, replace
from functools import cached_property

import numpy as np
import shapely
from shapely import affinity
from shapely.geometry import Polygon
from shapely.strtree import STRtree

from .packing import PackingSettings, pack_regions, require_in_range
from .refine import (
    SMALLEST_DETAIL,
    CleanupSettings,
    clean_regions,
    printability_warnings,
    scale_regions_to_mm,
    similar_fill,
)
from .overlay import (
    polygon_parts,
    robust_buffer,
    robust_difference,
    robust_simplify,
    robust_union,
    snapped,
    without_specks,
)
from .svg_parse import normalize_color, parse_svg, regions_from_fills

VOID_MODES = ('through', 'recess')
MAX_PAD_SIZE = 2000  # mm

# OpenSCAD works at about 0.000015 mm. A hole or sliver near that size
# collapses there, the extruded mesh comes out open, and OpenSCAD drops the
# shape from the render with nothing but "mesh is not closed" in its log.
# Outlines that touch at a single point do the same, and corners a few
# thousandths apart trip assertions in its geometry kernel. Set operations
# leave all three along every seam, so shapes that go into the .scad are put
# through _tidy and then sit on the OUTPUT_STEP grid, which is the precision
# the .scad is written at.
OUTPUT_STEP = 1e-4  # mm

PAD_LIMITS = {
    'target_width': (1, MAX_PAD_SIZE),
    'samples_per_segment': (2, 500),
    'base_thickness': (0.1, 100),
    'outline_offset': (0, 100),
    'outline_height': (0, 100),
    'pyramid_height': (0.1, 100),
    'recess_depth': (0, 100),
    'color_depth': (0.05, 100),
}


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

    # 'through' cuts enclosed holes through the pad. 'recess' leaves a floor
    # recess_depth below the surface the pyramids stand on.
    void_mode: str = 'through'
    recess_depth: float = 0.6

    # Every color other than the base owns the top color_depth of the floor
    # under its regions, plus their pyramids. The base is everything else:
    # the rest of the floor and the rim. base_color=None gives the base the
    # color that covers the most area, so it costs no extra filament.
    color_depth: float = 0.6
    base_color: str = None

    packing: PackingSettings = PackingSettings()
    cleanup: CleanupSettings = CleanupSettings()

    def __post_init__(self):
        if self.void_mode not in VOID_MODES:
            raise ValueError(f'void_mode must be one of {VOID_MODES}, got {self.void_mode!r}')
        require_in_range(self, PAD_LIMITS)
        if self.target_height is not None:
            require_in_range(self, {'target_height': (1, MAX_PAD_SIZE)})
        if self.base_color is not None:
            base_color = normalize_color(self.base_color)
            if base_color is None:
                raise ValueError('base color must be a color, not "none"')
            object.__setattr__(self, 'base_color', base_color)
        # A fixed order, so equal settings always have the same repr (the cache key).
        fills = (self.void_fills,) if isinstance(self.void_fills, str) else self.void_fills
        object.__setattr__(self, 'void_fills', tuple(sorted(fills)))

    @property
    def recesses_voids(self):
        return self.void_mode == 'recess' and self.recess_depth < self.base_thickness

    @classmethod
    def from_params(cls, params):
        """
        Settings from the flat name -> value mapping both apps collect from
        their forms. Missing names keep their defaults, a target_height of 0
        means no height limit, and an empty base_color means the largest color.
        """
        packing_names = {f.name for f in fields(PackingSettings)}
        own_names = {f.name for f in fields(cls)} - {'packing', 'cleanup'}
        unknown = set(params) - packing_names - own_names
        if unknown:
            raise ValueError(f'Unknown settings: {sorted(unknown)}')
        own = {name: value for name, value in params.items() if name in own_names}
        for optional in ('target_height', 'base_color'):
            if not own.get(optional):
                own[optional] = None
        packing = PackingSettings(**{n: v for n, v in params.items() if n in packing_names})
        return cls(packing=packing, **own)


@dataclass(frozen=True)
class Body:
    """One filament's share of the pad. A slicer gets each body as a separate part."""
    number: int  # 1 is the base. Doubles as the filament slot the body prints from.
    fill: str
    top: object  # floor area whose top layers the body owns; empty for the base, which owns the rest

    @property
    def is_base(self):
        return self.number == 1

    @property
    def label(self):
        return f'{self.number} base {self.fill}' if self.is_base else f'{self.number} {self.fill}'


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
        return self.settings.recesses_voids

    @property
    def color_depth(self):
        return min(self.settings.color_depth, self.settings.base_thickness)

    @cached_property
    def bodies(self):
        """The base first, then every other color from the largest area down."""
        area_by_fill = {}
        for region in self.regions:
            area_by_fill[region.fill] = area_by_fill.get(region.fill, 0.0) + region.geometry.area
        fills = sorted(area_by_fill, key=lambda fill: (-area_by_fill[fill], fill))
        base_fill = _base_fill(fills, self.settings)
        tops = [fill for fill in fills if fill != base_fill]
        return [Body(1, base_fill, Polygon())] + [
            Body(number, fill, _tidy(robust_union([r.geometry for r in self.regions if r.fill == fill])))
            for number, fill in enumerate(tops, start=2)
        ]

    def positions(self):
        return np.vstack([packed.positions for packed in self.packed.values()])

    def positions_by_body(self):
        """{body number: (N, 3) array of x, y, rotation} for the pyramids each body carries."""
        number_by_fill = {body.fill: body.number for body in self.bodies}
        grouped = {body.number: [np.empty((0, 3))] for body in self.bodies}
        for region in self.regions:
            grouped[number_by_fill[region.fill]].append(self.packed[region.id].positions)
        return {number: np.vstack(parts) for number, parts in grouped.items()}

    def preview(self):
        """The design as plain numbers and lists, for a frontend to draw."""
        packing = self.settings.packing
        return {
            'width': self.width,
            'height': self.height,
            'art': _ring_lists(self.art),
            'voids': _ring_lists(self.voids),
            'voids_recessed': self.voids_are_recessed,
            'skeleton': np.vstack([packed.skeleton for packed in self.packed.values()]).tolist(),
            'positions': self.positions().tolist(),
            'pyramid_size': packing.pyramid_size,
            'pyramid_sides': packing.pyramid_style,
            'warnings': list(self.warnings),
        }

    def describe(self):
        """Log lines: one summary, then every warning."""
        colors = len({region.fill for region in self.regions})
        summary = (f'  {len(self.regions)} region(s) in {colors} color(s), '
                   f'{self.width:.1f} x {self.height:.1f} mm, {len(self.positions())} pyramids')
        lines = [summary]
        if len(self.bodies) > 1:
            lines.append('  Filaments: ' + ', '.join(body.label for body in self.bodies))
        return lines + [f'  NOTE: {warning}' for warning in self.warnings]


def _base_fill(fills_by_area, settings):
    """
    The base body's color: the largest one unless base_color asks otherwise.
    A base_color that cleanup would have merged into one of the art's colors
    means that color, not an extra filament a shade away from it.
    """
    if settings.base_color is None:
        return fills_by_area[0]
    merged_into = similar_fill(settings.base_color, fills_by_area, settings.cleanup.color_tolerance)
    return merged_into or settings.base_color


def _ring_lists(geometry):
    """Each polygon as a list of rings of [x, y] points: the outline first, then its holes."""
    return [
        [[[float(x), float(y)] for x, y in ring.coords] for ring in (polygon.exterior, *polygon.interiors)]
        for polygon in polygon_parts(geometry)
    ]


def _as_one_color(regions):
    return regions_from_fills({regions[0].fill: robust_union([r.geometry for r in regions])})


def _fit_to_size(regions, target_width, target_height):
    """Cleanup can trim the extremes, so stretch back to the size that was asked for."""
    min_x, min_y, max_x, max_y = shapely.total_bounds([r.geometry for r in regions])
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
    grown = robust_buffer(geometry, radius, join_style='mitre')
    return robust_buffer(grown, -radius, join_style='mitre')


def _outlines_touch(geometry):
    """Whether two rings of the shape come closer than cleanup would ever leave a real gap."""
    rings = [
        ring for polygon in polygon_parts(geometry) for ring in (polygon.exterior, *polygon.interiors)
    ]
    if len(rings) < 2:
        return False
    one, other = STRtree(rings).query(rings, predicate='dwithin', distance=SMALLEST_DETAIL / 2)
    return bool((one != other).any())


def _tidy(geometry):
    """
    A shape OpenSCAD can extrude: nothing narrower than SMALLEST_DETAIL, no
    corners crowded together, and no outline touching another.

    Shaving off a neck regrows its two sides as sharp tips that meet at a
    point, as shapes drawn corner to corner already do. Only then is the
    whole shape drawn in by a hair, which pulls the tips apart. Any other
    shape keeps its exact outline.
    """
    cleared = robust_simplify(without_specks(geometry, SMALLEST_DETAIL), SMALLEST_DETAIL / 4)
    if _outlines_touch(cleared):
        cleared = robust_buffer(cleared, -SMALLEST_DETAIL / 4, join_style='mitre')
    return snapped(cleared, OUTPUT_STEP)


def _enclosed_holes(art):
    outlines = robust_union([Polygon(polygon.exterior) for polygon in polygon_parts(art)])
    return robust_difference(outlines, art)


def _assembly_notes(art, voids, settings):
    """What the rim does to the finished shape that the flat artwork doesn't show."""
    notes = []
    if settings.void_mode == 'recess' and not settings.recesses_voids:
        notes.append('Recess depth is at least the base thickness, so holes are cut through.')

    footprint_source = robust_union([art, voids]) if settings.recesses_voids else art
    if not settings.recesses_voids:
        filled_in = [
            void for void in polygon_parts(voids)
            if robust_buffer(void, -settings.outline_offset).is_empty
        ]
        if filled_in:
            notes.append(
                f'{len(filled_in)} hole(s) are narrower than twice the outline offset, so the rim '
                'fills them in. Recess the holes or reduce the outline offset to keep them.'
            )
    pieces = len(polygon_parts(robust_buffer(footprint_source, settings.outline_offset)))
    if pieces > 1:
        notes.append(f'The pad prints as {pieces} separate pieces.')
    return notes


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

    art = _tidy(_close_gaps(robust_union([r.geometry for r in regions]), settings.cleanup.min_feature / 2))
    voids = snapped(_enclosed_holes(art), OUTPUT_STEP)
    packed = pack_regions(regions, settings.packing)

    design = PadDesign(settings, regions, art, voids, packed, warnings)
    warnings += [warning.message for warning in printability_warnings(
        regions, settings.packing.pyramid_size, settings.packing.safety_margin,
        base_color=design.bodies[0].fill,
    )]
    warnings += _assembly_notes(art, voids, settings)
    if len(design.bodies) > 1 and settings.color_depth >= settings.base_thickness:
        warnings.append('Color depth is at least the base thickness, so colors run through the whole floor.')
    for line in design.describe():
        (logger or print)(line)
    return design
