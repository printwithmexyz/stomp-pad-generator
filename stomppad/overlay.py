"""The one place the package calls GEOS set operations and buffers.

GEOS reports trouble by throwing a C++ exception. On desktop that surfaces as
a Python GEOSException, and GEOS quietly recovers from many of its own (a
failed buffer retries at lower precision by catching what the first try
threw). In Pyodide's build nothing can catch a C++ exception: the first one
kills the whole runtime and the page has to be reloaded.

So nothing here gives GEOS the chance to throw:

- Union, difference and intersection run on a fixed-precision grid, the GEOS
  mode that is robust by construction. They only need valid input.
- Offsetting, simplifying and repairing invalid shapes do not use GEOS at
  all. Its buffer, its topology-preserving simplifier and make_valid can all
  throw (the simplifier does so depending on memory layout, so the same file
  may pass alone and fail in a batch). Clipper, the integer library slicers
  use, does those jobs and cannot fail that way.

The grid overlay has one known way to throw: input with detail about as fine
as the grid itself ("side location conflict"). Shapes that have been through
a cleanup radius never have any. Shapes stitched together from the scraps of
other set operations do, so they go through without_specks first.
"""

import math

import numpy as np
import pyclipper
import shapely
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.polygon import orient

GRID_FRACTION = 1e-7   # snap grid, as a fraction of the geometry's extent
ARC_FRACTION = 2e-5    # how far a rounded corner may stray from a true arc, same basis
ARC_TOLERANCE = ARC_FRACTION / GRID_FRACTION  # the same, in grid steps
CLIPPER_SAFE_RANGE = 1e15  # Clipper aborts the process a little past 4e18

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


class _IntegerGrid:
    """
    Maps coordinates to and from the integers Clipper works in. Clipper aborts
    the whole process outside its range, so the grid is anchored at the
    shape's own corner: the largest value is then about 1 / GRID_FRACTION
    however far the shape is from the origin.
    """

    def __init__(self, origin, step):
        self.origin = np.array(origin)
        self.scale = 1 / step

    @classmethod
    def around(cls, geometry, reach=0.0):
        """A grid sized to the geometry and anything within reach of it, or None if that is a point."""
        min_x, min_y, max_x, max_y = shapely.total_bounds([geometry])
        extent = max(max_x - min_x, max_y - min_y, abs(reach))
        if not (math.isfinite(extent) and extent > 0 and math.isfinite(1 / (extent * GRID_FRACTION))):
            return None
        return cls((min_x, min_y), extent * GRID_FRACTION)

    def to_grid(self, points):
        return np.round((np.asarray(points)[:, :2] - self.origin) * self.scale).astype(np.int64)

    def from_grid(self, contour):
        return np.array(contour) / self.scale + self.origin

    def ring_paths(self, geometry):
        """Every polygon ring as an integer path, shells and holes wound opposite ways."""
        paths = []
        for polygon in polygon_parts(geometry):
            polygon = orient(polygon)
            for ring in (polygon.exterior, *polygon.interiors):
                paths.append(self.to_grid(ring.coords[:-1]).tolist())
        return paths

    def polygons(self, tree):
        """Clipper's result tree alternates shells and holes level by level."""
        found = []
        for shell in tree.Childs:
            holes = [self.from_grid(hole.Contour) for hole in shell.Childs if len(hole.Contour) >= 3]
            if len(shell.Contour) >= 3:
                found.append(Polygon(self.from_grid(shell.Contour), holes))
            for hole in shell.Childs:
                found.extend(self.polygons(hole))
        return found

    def even_odd_area(self, paths):
        """The area the paths enclose an odd number of times, as clean polygons."""
        paths = [path for path in paths if len(path) >= 3]
        if not paths:
            return Polygon()
        clipper = pyclipper.Pyclipper()
        clipper.StrictlySimple = True
        try:
            clipper.AddPaths(paths, pyclipper.PT_SUBJECT, True)
        except pyclipper.ClipperException:  # every path was flat
            return Polygon()
        tree = clipper.Execute2(pyclipper.CT_UNION, pyclipper.PFT_EVENODD, pyclipper.PFT_EVENODD)
        polygons = self.polygons(tree)
        return polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)


def _repaired(geometry):
    # GEOS throws on NaN and infinite coordinates even in is_valid, so a shape
    # that has any is treated as nothing at all.
    if not _is_finite(geometry):
        return Polygon()
    if shapely.is_valid(geometry):
        return geometry
    grid = _IntegerGrid.around(geometry)
    return grid.even_odd_area(grid.ring_paths(geometry)) if grid else Polygon()


def snapped(geometry, step):
    """
    The geometry with every coordinate moved to a multiple of step, and no
    ring left touching or crossing itself at that resolution. A shape written
    out at a fixed number of decimals is then exactly the shape that was built.
    """
    if not _is_finite(geometry) or geometry.is_empty:
        return Polygon()
    if np.abs(shapely.total_bounds([geometry])).max() / step > CLIPPER_SAFE_RANGE:
        return Polygon()
    grid = _IntegerGrid((0.0, 0.0), step)
    return grid.even_odd_area(grid.ring_paths(geometry))


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


def _kept_by_douglas_peucker(points, tolerance):
    """Which points of an open polyline survive Douglas-Peucker simplification."""
    keep = np.zeros(len(points), dtype=bool)
    keep[[0, -1]] = True
    pending = [(0, len(points) - 1)]
    while pending:
        start, end = pending.pop()
        if end - start < 2:
            continue
        chord = points[end] - points[start]
        offsets = points[start + 1:end] - points[start]
        chord_length = np.hypot(*chord)
        if chord_length == 0:
            distances = np.hypot(offsets[:, 0], offsets[:, 1])
        else:
            distances = np.abs(chord[0] * offsets[:, 1] - chord[1] * offsets[:, 0]) / chord_length
        farthest = int(np.argmax(distances))
        if distances[farthest] > tolerance:
            middle = start + 1 + farthest
            keep[middle] = True
            pending += [(start, middle), (middle, end)]
    return keep


def _simplified_ring(points, tolerance):
    """A closed ring (first point not repeated) with stair-steps under tolerance removed."""
    opposite = int(np.argmax(np.hypot(*(points - points[0]).T)))
    if opposite == 0:
        return points[:1]
    there = points[:opposite + 1]
    back = np.vstack([points[opposite:], points[:1]])
    return np.vstack([
        there[_kept_by_douglas_peucker(there, tolerance)][:-1],
        back[_kept_by_douglas_peucker(back, tolerance)][:-1],
    ])


def robust_simplify(geometry, tolerance):
    """
    Remove vertices that move the outline by less than tolerance. Rings are
    simplified one at a time, so any that end up crossing are untangled.
    """
    grid = _IntegerGrid.around(geometry) if _is_finite(geometry) else None
    if grid is None:
        return Polygon()
    if tolerance <= 0:
        return geometry
    rings = [
        _simplified_ring(np.asarray(ring.coords)[:-1], tolerance)
        for polygon in polygon_parts(geometry)
        for ring in (polygon.exterior, *polygon.interiors)
    ]
    return grid.even_odd_area([grid.to_grid(ring).tolist() for ring in rings])


def without_specks(geometry, width):
    """
    Fill holes and gaps narrower than width, then shave off spikes, specks
    and necks narrower than width. Polygons that overlap or run alongside
    each other come back joined.

    Set operations leave debris a grid step wide along every seam they cut.
    A later overlay on a slightly different grid can throw on detail that
    fine, so a shape pieced together from such scraps goes through here
    before GEOS sees it again.
    """
    radius = width / 2
    filled = robust_buffer(robust_buffer(geometry, radius, 'mitre'), -radius, 'mitre')
    return robust_buffer(robust_buffer(filled, -radius, 'mitre'), radius, 'mitre')


def _lines(geometry, closed):
    """(points, closed) for every line in a line geometry."""
    for line in getattr(geometry, 'geoms', [geometry]):
        points = np.asarray(line.coords)
        line_closed = line.is_closed if closed is None else closed
        if line_closed and len(points) > 1 and (points[0] == points[-1]).all():
            points = points[:-1]
        yield points, line_closed


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
    # A zero-length line still has a size once it is given thickness.
    grid = _IntegerGrid.around(geometry, reach=distance)
    if grid is None:
        return Polygon()

    join = CLIPPER_JOINS[join_style]
    offsetter = pyclipper.PyclipperOffset(miter_limit=mitre_limit, arc_tolerance=ARC_TOLERANCE)
    if geometry.geom_type in ('LineString', 'LinearRing', 'MultiLineString'):
        for points, line_closed in _lines(geometry, closed):
            path = grid.to_grid(points)
            line_join = join
            if (path == path[0]).all():
                # A dot: SVG draws its cap and nothing else, and Clipper
                # shapes a lone point by its join.
                if cap_style == 'flat':
                    continue
                line_join = CLIPPER_JOINS['round' if cap_style == 'round' else 'bevel']
            end_type = pyclipper.ET_CLOSEDLINE if line_closed else CLIPPER_CAPS[cap_style]
            offsetter.AddPath(path.tolist(), line_join, end_type)
    else:
        for path in grid.ring_paths(geometry):
            offsetter.AddPath(path, join, pyclipper.ET_CLOSEDPOLYGON)

    polygons = grid.polygons(offsetter.Execute2(distance * grid.scale))
    return _repaired(polygons[0] if len(polygons) == 1 else MultiPolygon(polygons))
