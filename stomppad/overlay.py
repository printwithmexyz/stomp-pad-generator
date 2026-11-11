"""The one place the package calls GEOS set operations and buffers.

GEOS reports trouble by throwing a C++ exception. On desktop that surfaces as
a Python GEOSException, and GEOS quietly recovers from many of its own (a
failed buffer retries at lower precision by catching what the first try
threw). In Pyodide's build nothing can catch a C++ exception: the first one
kills the whole runtime and the page has to be reloaded.

So nothing here gives GEOS the chance to throw:

- Union, difference and intersection run on a fixed-precision grid, the GEOS
  mode that is robust by construction. They only need valid input, which
  they repair if they have to.
- Buffers do not use GEOS at all. GEOS has no exception-free buffer, and with
  it about 3 in 100 real logos killed the runtime. Offsetting is done by
  Clipper, the integer library slicers use, which cannot fail that way.
"""

import math

import numpy as np
import pyclipper
import shapely
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.polygon import orient

GRID_FRACTION = 1e-7   # snap grid, as a fraction of the geometry's extent
ARC_FRACTION = 2e-5    # how far a rounded corner may stray from a true arc, same basis

CLIPPER_JOINS = {
    'round': pyclipper.JT_ROUND,
    'mitre': pyclipper.JT_MITER,
    # Clipper has no true bevel. Its square join cuts the corner one offset
    # distance out, so a bevelled stroke corner comes out slightly fuller.
    'bevel': pyclipper.JT_SQUARE,
}
CLIPPER_CAPS = {
    'round': pyclipper.ET_OPENROUND,
    'flat': pyclipper.ET_OPENBUTT,
    'square': pyclipper.ET_OPENSQUARE,
}


def polygon_parts(geometry):
    """Every non-empty polygon inside a geometry, however deeply it is nested."""
    if geometry.geom_type == 'Polygon':
        return [] if geometry.is_empty else [geometry]
    return [part for g in getattr(geometry, 'geoms', []) for part in polygon_parts(g)]


def _is_finite(geometry):
    return bool(np.isfinite(shapely.get_coordinates(geometry)).all())


def _repaired(geometry):
    # GEOS throws on NaN and infinite coordinates even in is_valid, so a shape
    # that has any is treated as nothing at all.
    if not _is_finite(geometry):
        return Polygon()
    if shapely.is_valid(geometry):
        return geometry
    return MultiPolygon(polygon_parts(shapely.make_valid(geometry)))


def _on_a_grid(geometries):
    """Valid inputs, plus the grid size to overlay them on (None if they have no extent)."""
    valid = [_repaired(g) for g in geometries]
    min_x, min_y, max_x, max_y = shapely.total_bounds(valid)
    extent = max(max_x - min_x, max_y - min_y)
    return valid, extent * GRID_FRACTION if math.isfinite(extent) and extent > 0 else None


def robust_union(geometries):
    geometries = list(geometries)
    if not geometries:
        return Polygon()
    valid, grid_size = _on_a_grid(geometries)
    return shapely.union_all(valid, grid_size=grid_size)


def robust_difference(geometry, cutter):
    (geometry, cutter), grid_size = _on_a_grid([geometry, cutter])
    return shapely.difference(geometry, cutter, grid_size=grid_size)


def robust_intersection(geometry, other):
    (geometry, other), grid_size = _on_a_grid([geometry, other])
    return shapely.intersection(geometry, other, grid_size=grid_size)


def _clipper_paths(geometry, closed):
    """(points, closed, is_line) for every ring or line Clipper should offset."""
    if geometry.geom_type in ('LineString', 'LinearRing', 'MultiLineString'):
        for line in getattr(geometry, 'geoms', [geometry]):
            points = np.asarray(line.coords)
            line_closed = line.is_closed if closed is None else closed
            if line_closed and len(points) > 1 and (points[0] == points[-1]).all():
                points = points[:-1]
            yield points, line_closed, True
        return
    for polygon in polygon_parts(geometry):
        # Clipper tells shells from holes by winding direction.
        polygon = orient(polygon)
        for ring in (polygon.exterior, *polygon.interiors):
            yield np.asarray(ring.coords)[:-1], True, False


def _polygons_from_tree(node, from_grid):
    """Clipper's result tree alternates shells and holes level by level."""
    polygons = []
    for shell in node.Childs:
        holes = [from_grid(hole.Contour) for hole in shell.Childs if len(hole.Contour) >= 3]
        if len(shell.Contour) >= 3:
            polygons.append(Polygon(from_grid(shell.Contour), holes))
        for hole in shell.Childs:
            polygons.extend(_polygons_from_tree(hole, from_grid))
    return polygons


def robust_buffer(geometry, distance, join_style='round', cap_style='round', mitre_limit=5.0,
                  closed=None):
    """
    Grow (or with a negative distance, shrink) a polygon, or give a line
    thickness. Takes the same style names as shapely's buffer, with two
    differences that come from Clipper: a mitre longer than mitre_limit is cut
    off one distance from the corner, not mitre_limit distances, and 'bevel'
    cuts the corner one distance out where a true bevel cuts it flush.

    closed says whether a line's ends are joined. Left as None, a line whose
    first and last points coincide counts as closed.
    """
    if not (_is_finite(geometry) and math.isfinite(distance)):
        return Polygon()
    min_x, min_y, max_x, max_y = shapely.total_bounds([geometry])
    # A zero-length line still has a size once it is given thickness.
    extent = max(max_x - min_x, max_y - min_y, abs(distance))
    scale = 1 / (extent * GRID_FRACTION) if extent > 0 else math.inf
    if not math.isfinite(scale):
        return Polygon()

    # Clipper works in integers and aborts the process outside its range, so
    # the grid is anchored at the shape's own corner. The largest value is
    # then about 1 / GRID_FRACTION however far the shape is from the origin.
    origin = np.array([min_x, min_y])
    offsetter = pyclipper.PyclipperOffset(
        miter_limit=mitre_limit, arc_tolerance=extent * ARC_FRACTION * scale,
    )
    for points, path_closed, is_line in _clipper_paths(geometry, closed):
        path = np.round((points[:, :2] - origin) * scale).astype(np.int64)
        join, end_type = CLIPPER_JOINS[join_style], pyclipper.ET_CLOSEDPOLYGON
        if is_line:
            end_type = pyclipper.ET_CLOSEDLINE if path_closed else CLIPPER_CAPS[cap_style]
            if (path == path[0]).all():
                # A dot: SVG draws its cap and nothing else, and Clipper
                # shapes a lone point by its join.
                if cap_style == 'flat':
                    continue
                join = CLIPPER_JOINS['round' if cap_style == 'round' else 'bevel']
        offsetter.AddPath(path.tolist(), join, end_type)

    def from_grid(contour):
        return np.array(contour) / scale + origin

    polygons = _polygons_from_tree(offsetter.Execute2(distance * scale), from_grid)
    return _repaired(polygons[0] if len(polygons) == 1 else MultiPolygon(polygons))
