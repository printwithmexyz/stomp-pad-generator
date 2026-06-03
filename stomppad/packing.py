"""Pyramid packing — skeleton, footprint, and validated hex placement.

Phase 1.2 split this out of the monolith and hardened two long-standing
issues called out in the v2 plan:

- Per-point ``polygon_safe.contains(footprint)`` is now
  ``prepared.prep(polygon_safe).contains(footprint)``. ``prep`` builds an
  RTree of the polygon's edges once, so each subsequent contains test is
  near-constant-time instead of O(edges). On the smiley fixture the hot
  loop is dominated by ~hundreds of contains tests; for big multi-body
  files the difference is "fast" vs "Pyodide tab freezes for seconds".

- :class:`PackContext` caches per-component skeletons (the medial-axis
  raster step) keyed by component id, so Phase 2's "re-pack only edited
  bodies" can flow without recomputing every untouched body's skeleton.
  Cache is invalidated by :meth:`PackContext.invalidate`.

The legacy ``calculate_valid_pyramid_positions(polygon, ...)`` keeps the
same signature as the Phase 0 version so the desktop GUI's existing
single-polygon call path and the regression-fixture pipeline both still
land here.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import shapely
from shapely.geometry import Point, Polygon
from shapely.prepared import prep
from skimage.morphology import medial_axis

from .geometry import Component


def _log(logger, message):
    (logger or print)(message)


def calculate_skeleton(polygon, resolution: float = 0.5):
    """Medial-axis skeleton of ``polygon`` as a list of ``(x, y)`` points.

    Vectorized: ``shapely.contains_xy`` over a meshgrid is ~50–100x faster
    than ``polygon.contains(Point(x, y))`` per pixel, which matters in
    Pyodide where Python loops are uncached interpreted bytecode.
    """
    min_x, min_y, max_x, max_y = polygon.bounds
    width = int((max_x - min_x) / resolution) + 1
    height = int((max_y - min_y) / resolution) + 1

    grid_x = min_x + np.arange(width) * resolution
    grid_y = min_y + np.arange(height) * resolution
    xx, yy = np.meshgrid(grid_x, grid_y)
    binary_image = shapely.contains_xy(polygon, xx.ravel(), yy.ravel()).reshape(height, width)

    skeleton = medial_axis(binary_image)

    iy, jx = np.where(skeleton)
    xs = min_x + jx * resolution
    ys = min_y + iy * resolution
    return list(zip(xs.tolist(), ys.tolist()))


def calculate_centerline_tangent(skeleton_points, x: float, y: float,
                                 sample_distance: float = 3.0) -> float:
    """Tangent (in degrees) of the centerline near ``(x, y)``.

    PCA over nearby skeleton points; ``eigh`` is faster and more stable
    than the general eigendecomposition for the symmetric covariance.
    """
    if not skeleton_points:
        return 0.0

    point = np.array([x, y])
    skeleton_array = np.array(skeleton_points)
    distances = np.linalg.norm(skeleton_array - point, axis=1)
    nearby_indices = np.where(distances < sample_distance)[0]

    if len(nearby_indices) < 2:
        return 0.0

    nearby_points = skeleton_array[nearby_indices]
    centered = nearby_points - nearby_points.mean(axis=0)
    cov = np.cov(centered.T)
    eigenvalues, eigenvectors = np.linalg.eigh(cov)
    principal_direction = eigenvectors[:, np.argmax(eigenvalues)]
    return np.degrees(np.arctan2(principal_direction[1], principal_direction[0]))


def create_pyramid_footprint(x: float, y: float, pyramid_size: float,
                             rotation_deg: float = 0) -> Polygon:
    """Square pyramid base rotated 45° + ``rotation_deg`` around ``(x, y)``."""
    half_size = pyramid_size / 2
    base_angle = 45
    total_rotation = base_angle + rotation_deg

    corners = []
    for angle_offset in [0, 90, 180, 270]:
        angle_rad = np.radians(total_rotation + angle_offset)
        corner_dist = half_size * np.sqrt(2)
        corner_x = x + corner_dist * np.cos(angle_rad)
        corner_y = y + corner_dist * np.sin(angle_rad)
        corners.append((corner_x, corner_y))
    return Polygon(corners)


class PackContext:
    """Per-component skeleton cache.

    Reuse one ``PackContext`` across calls so re-pack on edits doesn't
    rebuild skeletons for untouched bodies. Phase 2's editor instantiates
    one per ShapeProject; the batch pipeline gets a fresh one per file.
    """

    def __init__(self) -> None:
        self._skeleton_cache: dict[int, list[tuple[float, float]]] = {}

    def invalidate(self, component_id: int) -> None:
        self._skeleton_cache.pop(component_id, None)

    def clear(self) -> None:
        self._skeleton_cache.clear()

    def skeleton_for(self, component: Component, resolution: float) -> list[tuple[float, float]]:
        cached = self._skeleton_cache.get(component.id)
        if cached is not None:
            return cached
        solid = component.to_polygon()
        skel = calculate_skeleton(solid, resolution=resolution)
        self._skeleton_cache[component.id] = skel
        return skel


def calculate_valid_pyramid_positions(
        polygon,
        pyramid_size: float = 4,
        pyramid_spacing: float = 1,
        target_width: float = 100,
        include_rotation: bool = True,
        safety_margin: float = 0.5,
        skeleton_points=None,
        skeleton_resolution: float = 0.3,
        logger=None,
):
    """Skeleton-following hex pack of ``polygon``.

    Back-compat surface: the Phase 0 signature is preserved so the desktop
    GUI's existing ``import ...`` and the web ``main.js`` Python step keep
    working unchanged. Internally this now uses ``prepared.prep`` for
    repeated contains tests.
    """
    if skeleton_points is None:
        _log(logger, "  Calculating skeleton/centerline...")
        skeleton_points = calculate_skeleton(polygon, resolution=skeleton_resolution)

    if not skeleton_points:
        _log(logger, "  WARNING: No skeleton points found")
        return []

    _log(logger, f"  Found {len(skeleton_points)} skeleton points")

    polygon_safe = polygon.buffer(-safety_margin) if safety_margin > 0 else polygon
    if polygon_safe.is_empty:
        _log(logger, "  WARNING: polygon collapsed under safety margin; nothing to pack")
        return []
    safe_prep = prep(polygon_safe)

    pyramid_pitch = pyramid_size + pyramid_spacing
    hex_spacing_x = pyramid_pitch
    hex_spacing_y = pyramid_pitch * np.sqrt(3) / 2

    bounds = polygon.bounds
    min_x, min_y, max_x, max_y = bounds
    num_rows = int(np.ceil((max_y - min_y) / hex_spacing_y)) + 2
    num_cols = int(np.ceil((max_x - min_x) / hex_spacing_x)) + 2

    _log(logger, f"  Testing {num_rows * num_cols} potential positions...")

    valid_positions = []
    for row in range(num_rows):
        x_offset = (row % 2) * (hex_spacing_x / 2)
        for col in range(num_cols):
            x_pos = min_x + col * hex_spacing_x + x_offset
            y_pos = min_y + row * hex_spacing_y

            if not safe_prep.contains(Point(x_pos, y_pos)):
                continue

            rotation = (
                calculate_centerline_tangent(skeleton_points, x_pos, y_pos)
                if include_rotation
                else 0
            )
            footprint = create_pyramid_footprint(x_pos, y_pos, pyramid_size, rotation)
            if safe_prep.contains(footprint):
                valid_positions.append(
                    [x_pos, y_pos, rotation] if include_rotation else [x_pos, y_pos]
                )

    _log(logger, f"  Valid positions: {len(valid_positions)}")
    return valid_positions


def pack_component(
    component: Component,
    *,
    pyramid_size: float = 4,
    pyramid_spacing: float = 1,
    safety_margin: float = 0.5,
    include_rotation: bool = True,
    skeleton_resolution: float = 0.3,
    context: Optional[PackContext] = None,
    pattern: str = "skeleton",
    logger=None,
):
    """Pack pyramids inside a single :class:`Component` (avoiding holes).

    Resolves ``pattern`` against the :mod:`stomppad.patterns` registry and
    delegates position generation there. The strategy receives the
    component's solid geometry (exterior minus holes) and the merged
    params; the validation pass (footprint inside safety-inset polygon)
    happens here once for consistency across patterns.

    ``context`` is optional. If omitted, a one-shot ``PackContext`` is
    created — fine for ad-hoc calls; pass a long-lived one when re-packing
    an edited :class:`ShapeProject` so untouched bodies' skeletons stay
    cached.
    """
    if context is None:
        context = PackContext()

    from . import patterns  # local to avoid import cycles at module load

    solid = component.to_polygon()
    polygon_safe = solid.buffer(-safety_margin) if safety_margin > 0 else solid
    if polygon_safe.is_empty:
        _log(logger, f"  WARNING: component {component.id} collapsed under safety margin")
        return []
    safe_prep = prep(polygon_safe)

    strategy = patterns.get(pattern)
    skeleton_points = (
        context.skeleton_for(component, skeleton_resolution)
        if strategy.needs_skeleton
        else None
    )

    # Hand the strategy the safety-inset polygon so its grid bounds match
    # the actual valid region — generating candidates against the un-inset
    # solid and relying on the prep-contains filter to reject the fringe
    # is correct but wasteful (proportional to margin/extent).
    candidates = strategy.generate_positions(
        solid_geom=polygon_safe,
        pyramid_size=pyramid_size,
        pyramid_spacing=pyramid_spacing,
        include_rotation=include_rotation,
        skeleton_points=skeleton_points,
        logger=logger,
    )

    valid = []
    for pos in candidates:
        x, y = pos[0], pos[1]
        rot = pos[2] if len(pos) > 2 else 0
        if not safe_prep.contains(Point(x, y)):
            continue
        footprint = create_pyramid_footprint(x, y, pyramid_size, rot)
        if safe_prep.contains(footprint):
            valid.append([x, y, rot] if include_rotation else [x, y])
    _log(logger, f"  Component {component.id}: {len(valid)} pyramids")
    return valid
