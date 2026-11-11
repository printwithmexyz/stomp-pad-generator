"""Scale parsed regions to millimetres, clean them up, and flag what won't print well."""

import math
from dataclasses import dataclass, replace

import numpy as np
import shapely
from shapely import affinity
from shapely.geometry import GeometryCollection, Polygon
from shapely.geometry.polygon import orient

from .overlay import (
    polygon_parts,
    robust_buffer,
    robust_difference,
    robust_intersection,
    robust_simplify,
    robust_union,
    without_specks,
)
from .svg_parse import normalize_color, regions_from_fills


TIP_MITRE_LIMIT = 50  # lets the regrow step rebuild tips as sharp as about 2 degrees
ENCLOSED_FRACTION = 0.8  # share of a removed piece's outline that kept colors must border to absorb it
SMALLEST_DETAIL = 0.01   # mm; nothing narrower survives in a shape pieced together from set operations


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


def similar_fill(fill, candidates, tolerance):
    """The first candidate within tolerance of fill on every channel, or None."""
    return next(
        (candidate for candidate in candidates
         if max(abs(a - b) for a, b in zip(_channels(candidate), _channels(fill))) <= tolerance),
        None,
    )


def merge_similar_fills(regions, tolerance):
    """Fold near-identical fills (#000000 vs #010102) into the one covering more area."""
    area_by_fill = {}
    for region in regions:
        area_by_fill[region.fill] = area_by_fill.get(region.fill, 0.0) + region.geometry.area

    kept, remap = [], {}
    for fill in sorted(area_by_fill, key=area_by_fill.get, reverse=True):
        match = similar_fill(fill, kept, tolerance)
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
    simplified = robust_simplify(geometry, settings.simplify_tolerance)
    return _printable_pieces(_open(simplified, settings.min_feature / 2), settings)


def _outline_samples(polygon, spacing, offset):
    """
    Points just outside a polygon, offset from its outline and from the rims
    of its holes, about spacing apart. Returns (points, lengths), where each
    length is the stretch of outline its point stands for.
    """
    polygon = orient(polygon)
    points, lengths = [], []
    for ring in (polygon.exterior, *polygon.interiors):
        corners = np.asarray(ring.coords)[:, :2]
        steps = np.diff(corners, axis=0)
        edge_lengths = np.hypot(steps[:, 0], steps[:, 1])
        real = edge_lengths > 0
        starts, steps, edge_lengths = corners[:-1][real], steps[real], edge_lengths[real]
        counts = np.maximum(1, np.ceil(edge_lengths / spacing).astype(int))
        edge = np.repeat(np.arange(len(counts)), counts)
        nth = np.arange(counts.sum()) - np.repeat(np.cumsum(counts) - counts, counts)
        along = (nth + 0.5) / counts[edge]
        # With the outline counter-clockwise and holes clockwise, the right of
        # every edge is outside the polygon.
        outward = np.column_stack([steps[:, 1], -steps[:, 0]]) / edge_lengths[:, None]
        points.append(starts[edge] + steps[edge] * along[:, None] + outward[edge] * offset)
        lengths.append((edge_lengths / counts)[edge])
    return np.vstack(points), np.concatenate(lengths)


def _removed_between_colors(painted, kept, settings):
    """
    The pieces cleanup removed from where kept colors face each other across
    a gap too small to print. Whatever it removed from the outer edge is not
    among them, even where a sliver runs on past the colors it separates.
    """
    reach = max(settings.min_feature / 2, math.sqrt(settings.min_area / math.pi))
    closed = robust_buffer(robust_buffer(kept, reach, 'mitre'), -reach, 'mitre')
    gaps = without_specks(robust_difference(closed, kept), SMALLEST_DETAIL)
    return polygon_parts(robust_intersection(gaps, painted))


def _bordering_lengths(pieces, geometry_by_fill, spacing):
    """
    How much of each piece's outline each color lies along, as a
    (colors, pieces) array, plus each piece's whole outline length. Measured
    with point tests, because the pieces are scraps too fine for an overlay.
    """
    sampled = [_outline_samples(piece, spacing, offset=spacing / 4) for piece in pieces]
    points = np.vstack([points for points, _ in sampled])
    lengths = np.concatenate([lengths for _, lengths in sampled])
    piece_of = np.repeat(np.arange(len(pieces)), [len(lengths) for _, lengths in sampled])
    bordered = []
    for geometry in geometry_by_fill.values():
        shapely.prepare(geometry)
        touching = shapely.contains_xy(geometry, points[:, 0], points[:, 1])
        bordered.append(np.bincount(piece_of, lengths * touching, minlength=len(pieces)))
    return np.array(bordered), np.bincount(piece_of, lengths, minlength=len(pieces))


def _absorb_enclosed(painted, cleaned_by_fill, settings):
    """
    Hand each removed piece that kept colors surround to the color it borders
    most. Left alone, a hairline between two colors would show the base color
    and a speck where colors meet would become a hole through the pad.
    """
    if not cleaned_by_fill:
        return cleaned_by_fill
    kept = robust_union(cleaned_by_fill.values())
    pieces = _removed_between_colors(painted, kept, settings)
    if not pieces:
        return cleaned_by_fill

    spacing = max(settings.min_feature / 2, SMALLEST_DETAIL)
    bordered, outline = _bordering_lengths(pieces, cleaned_by_fill, spacing)
    enclosed = (outline > 0) & (bordered.sum(axis=0) >= ENCLOSED_FRACTION * outline)
    winners = bordered.argmax(axis=0)

    absorbed = dict(cleaned_by_fill)
    for index, fill in enumerate(cleaned_by_fill):
        won = [piece for piece, keep in zip(pieces, enclosed & (winners == index)) if keep]
        if won:
            absorbed[fill] = without_specks(
                GeometryCollection([cleaned_by_fill[fill], *won]), SMALLEST_DETAIL,
            )
    return absorbed


def clean_regions(regions, settings=CleanupSettings()):
    """
    Merge near-duplicate fills, smooth traced edges, and drop slivers, specks
    and pinholes too small to print. Expects millimetres. Colors do not
    overlap afterwards by more than a few thousandths of a square millimetre:
    where edges were smoothed past each other, the color cleaned first keeps
    the overlap. Removed detail that other colors surround is absorbed by a
    neighbour, so it leaves no gap.
    Returns (regions, notes) where notes describe what was removed.
    """
    merged = merge_similar_fills(regions, settings.color_tolerance)
    notes = []
    fills_before = len({r.fill for r in regions})
    fills_after = len({r.fill for r in merged})
    if fills_after < fills_before:
        notes.append(f'Merged {fills_before - fills_after} near-duplicate color(s).')

    painted_by_fill = _geometry_by_fill(merged)
    cleaned_by_fill = {}
    for fill, geometry in painted_by_fill.items():
        cleaned = _clean_geometry(geometry, settings)
        if cleaned_by_fill and not cleaned.is_empty:
            taken = robust_union(cleaned_by_fill.values())
            cleaned = _printable_pieces(robust_difference(cleaned, taken), settings)
        if not cleaned.is_empty:
            cleaned_by_fill[fill] = cleaned
    absorbed_by_fill = _absorb_enclosed(robust_union(painted_by_fill.values()), cleaned_by_fill, settings)
    # Joining a piece to its neighbour can leave pinholes along the seam.
    cleaned_by_fill = {
        fill: _printable_pieces(geometry, settings) for fill, geometry in absorbed_by_fill.items()
    }

    for fill, geometry in painted_by_fill.items():
        if fill not in cleaned_by_fill:
            notes.append(f'Removed {fill} entirely: every piece was too small to print.')
            continue
        removed_area = geometry.area - cleaned_by_fill[fill].area
        if removed_area > settings.min_area:
            notes.append(f'Trimmed {removed_area:.1f} mm^2 of unprintable detail from {fill}.')
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


def printability_warnings(regions, pyramid_size, safety_margin=0.5, max_colors=4, flat_fraction=0.4,
                          base_color=None):
    """
    Flag colors beyond the AMS count and regions too narrow to carry grip
    pyramids. A base_color no region uses counts as one more filament.
    """
    warnings = []
    fills = set(region.fill for region in regions) | ({base_color} if base_color else set())
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
