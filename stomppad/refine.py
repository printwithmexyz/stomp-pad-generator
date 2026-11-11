"""Scale parsed regions to millimetres, clean them up, and flag what won't print well."""

from dataclasses import dataclass, replace

from shapely import affinity
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient

import shapely

from .overlay import (
    polygon_parts,
    robust_buffer,
    robust_difference,
    robust_intersection,
    robust_union,
)
from .svg_parse import normalize_color, regions_from_fills


TIP_MITRE_LIMIT = 50  # lets the regrow step rebuild tips as sharp as about 2 degrees


@dataclass(frozen=True)
class CleanupSettings:
    min_feature: float = 0.4          # mm, thinnest wall worth printing (about one nozzle line)
    simplify_tolerance: float = 0.05  # mm, removes stair-steps from traced artwork
    min_area: float = 1.0             # mm^2, smaller specks and pinholes are dropped
    color_tolerance: int = 16         # per-channel distance below which fills are merged


@dataclass(frozen=True)
class PrintWarning:
    code: str
    message: str
    region_id: int = None


def _geometry_by_fill(regions):
    grouped = {}
    for region in regions:
        grouped.setdefault(region.fill, []).append(region.geometry)
    return {fill: robust_union(parts) for fill, parts in grouped.items()}


def scale_regions_to_mm(regions, target_width, target_height=None):
    """
    Scale regions to fit the target size and flip to Y-up with the artwork's
    lower-left corner at the origin. Returns (regions, scale_factor).

    Width alone sets the scale. With a height as well, the artwork fits inside
    both and keeps its aspect ratio. Exteriors come back counter-clockwise,
    since the flip would otherwise reverse them.
    """
    if not regions:
        raise ValueError('No regions to scale.')
    min_x, min_y, max_x, max_y = shapely.total_bounds([r.geometry for r in regions])
    scale = target_width / (max_x - min_x)
    if target_height:
        scale = min(scale, target_height / (max_y - min_y))
    matrix = [scale, 0, 0, -scale, -min_x * scale, max_y * scale]
    scaled = [
        replace(region, geometry=orient(affinity.affine_transform(region.geometry, matrix)))
        for region in regions
    ]
    return scaled, scale


def _channels(fill):
    return tuple(int(fill[i:i + 2], 16) for i in (1, 3, 5))


def merge_similar_fills(regions, tolerance):
    """Fold near-identical fills (#000000 vs #010102) into the one covering more area."""
    area_by_fill = {}
    for region in regions:
        area_by_fill[region.fill] = area_by_fill.get(region.fill, 0.0) + region.geometry.area

    kept, remap = [], {}
    for fill in sorted(area_by_fill, key=area_by_fill.get, reverse=True):
        match = next(
            (k for k in kept
             if max(abs(a - b) for a, b in zip(_channels(k), _channels(fill))) <= tolerance),
            None,
        )
        remap[fill] = match or fill
        if match is None:
            kept.append(fill)

    if all(source == target for source, target in remap.items()):
        return regions
    return regions_from_fills(
        _geometry_by_fill([replace(r, fill=remap[r.fill]) for r in regions])
    )


def _open(geometry, radius):
    """
    Erase anything thinner than 2 * radius. Growing back with long mitres
    rebuilds sharp tips, and clipping to the input stops them overshooting.
    """
    shrunk = robust_buffer(geometry, -radius, join_style='mitre')
    regrown = robust_buffer(shrunk, radius, join_style='mitre', mitre_limit=TIP_MITRE_LIMIT)
    return robust_intersection(regrown, geometry)


def _without_pinholes(polygon, min_area):
    holes = [ring for ring in polygon.interiors if Polygon(ring).area >= min_area]
    return Polygon(polygon.exterior, holes)


def _printable_pieces(geometry, settings):
    pieces = [p for p in polygon_parts(geometry) if p.area >= settings.min_area]
    return robust_union([_without_pinholes(p, settings.min_area) for p in pieces])


def _clean_geometry(geometry, settings):
    simplified = geometry.simplify(settings.simplify_tolerance, preserve_topology=True)
    return _printable_pieces(_open(simplified, settings.min_feature / 2), settings)


def clean_regions(regions, settings=CleanupSettings()):
    """
    Merge near-duplicate fills, smooth traced edges, and drop slivers, specks
    and pinholes too small to print. Expects millimetres. Colors never
    overlap afterwards: where edges were smoothed past each other, the color
    cleaned first keeps the overlap.
    Returns (regions, notes) where notes describe what was removed.
    """
    merged = merge_similar_fills(regions, settings.color_tolerance)
    notes = []
    fills_before = len({r.fill for r in regions})
    fills_after = len({r.fill for r in merged})
    if fills_after < fills_before:
        notes.append(f'Merged {fills_before - fills_after} near-duplicate color(s).')

    cleaned_by_fill = {}
    for fill, geometry in _geometry_by_fill(merged).items():
        cleaned = _clean_geometry(geometry, settings)
        if cleaned_by_fill and not cleaned.is_empty:
            taken = robust_union(cleaned_by_fill.values())
            cleaned = _printable_pieces(robust_difference(cleaned, taken), settings)
        removed_area = geometry.area - cleaned.area
        if cleaned.is_empty:
            notes.append(f'Removed {fill} entirely: every piece was too small to print.')
            continue
        if removed_area > settings.min_area:
            notes.append(f'Trimmed {removed_area:.1f} mm^2 of unprintable detail from {fill}.')
        cleaned_by_fill[fill] = cleaned
    return regions_from_fills(cleaned_by_fill), notes


def apply_overrides(regions, drop_ids=(), recolor=None):
    """
    Manual tweaks: drop regions by id and recolor regions ({id: color}).
    Recoloring to 'none' drops the region. Neighbours that end up the same
    color are joined, so ids are renumbered.
    """
    recolor = {region_id: normalize_color(color) for region_id, color in (recolor or {}).items()}
    dropped = set(drop_ids) | {region_id for region_id, color in recolor.items() if color is None}
    edited = [
        replace(region, fill=recolor.get(region.id, region.fill))
        for region in regions
        if region.id not in dropped
    ]
    return regions_from_fills(_geometry_by_fill(edited))


def printability_warnings(regions, pyramid_size, safety_margin=0.5, max_colors=4, flat_fraction=0.4):
    """Flag colors beyond the AMS count and regions too narrow to carry grip pyramids."""
    warnings = []
    fills = list(dict.fromkeys(region.fill for region in regions))
    if len(fills) > max_colors:
        warnings.append(PrintWarning(
            'too_many_colors',
            f'{len(fills)} colors found but only {max_colors} filaments are available. '
            'Merge or recolor some regions.',
        ))

    clearance = pyramid_size / 2 + safety_margin
    for region in regions:
        label = f'Region {region.id} ({region.fill}, {region.geometry.area:.0f} mm^2)'
        reachable = robust_buffer(region.geometry, -clearance)
        if reachable.is_empty:
            warnings.append(PrintWarning(
                'no_grip', f'{label} is too narrow for any pyramid and will print flat.', region.id,
            ))
            continue
        flat = 1 - robust_buffer(reachable, clearance).area / region.geometry.area
        if flat > flat_fraction:
            warnings.append(PrintWarning(
                'partial_grip',
                f'{label} is too narrow for pyramids across about {flat:.0%} of its area.',
                region.id,
            ))
    return warnings
