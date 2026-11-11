"""The one place the package calls GEOS set operations and buffers.

GEOS reports trouble by throwing a C++ exception. On desktop that surfaces as
a Python GEOSException, and GEOS quietly recovers from many of its own (a
failed buffer retries at lower precision by catching what the first try
threw). In Pyodide's build nothing can catch a C++ exception: the first one
kills the whole runtime and the page has to be reloaded.

Union, difference and intersection here cannot raise. They run on a
fixed-precision grid, the GEOS mode that is robust by construction, and only
need valid input, which they repair if they have to.

Buffers are NOT safe yet. GEOS has no exception-free buffer, so in Pyodide
about 3 in 100 real logos still kill the runtime inside robust_buffer. See
"Known gaps" in docs/v2-plan.md before changing it.
"""

import math

import shapely
from shapely.geometry import MultiPolygon, Polygon

GRID_FRACTION = 1e-7  # snap grid, as a fraction of the geometry's extent


def polygon_parts(geometry):
    """Every non-empty polygon inside a geometry, however deeply it is nested."""
    if geometry.geom_type == 'Polygon':
        return [] if geometry.is_empty else [geometry]
    return [part for g in getattr(geometry, 'geoms', []) for part in polygon_parts(g)]


def _repaired(geometry):
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


def robust_buffer(geometry, distance, **style):
    """
    geometry.buffer(distance, **style). Reliable on desktop, where GEOS recovers
    from its own failures. In Pyodide it is the last operation that can crash.
    """
    return geometry.buffer(distance, **style)
