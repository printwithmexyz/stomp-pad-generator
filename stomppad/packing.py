"""Place grip pyramids inside regions on a hex grid, turned to follow each region's centerline."""

from dataclasses import dataclass

import numpy as np
import shapely
from scipy.spatial import cKDTree
from skimage.morphology import medial_axis

TANGENT_SAMPLE_DISTANCE = 3.0  # mm of centerline used to judge its direction


@dataclass(frozen=True)
class PackingSettings:
    pyramid_size: float = 4.0
    pyramid_spacing: float = 1.0
    safety_margin: float = 0.5
    include_rotation: bool = True
    skeleton_resolution: float = 0.3


@dataclass
class PackedRegion:
    positions: np.ndarray  # (N, 3): x, y, rotation in degrees
    skeleton: np.ndarray   # (M, 2) centerline points, kept for previews


def _medial_axis(mask):
    """Seeded, because the algorithm breaks ties at random and placements must be repeatable."""
    try:
        return medial_axis(mask, rng=0)
    except TypeError:  # scikit-image < 0.21 calls the seed random_state
        return medial_axis(mask, random_state=0)


def calculate_skeleton(geometry, resolution):
    """Medial-axis points of the geometry as an (N, 2) array."""
    min_x, min_y, max_x, max_y = geometry.bounds
    grid_x = min_x + np.arange(int((max_x - min_x) / resolution) + 1) * resolution
    grid_y = min_y + np.arange(int((max_y - min_y) / resolution) + 1) * resolution
    xx, yy = np.meshgrid(grid_x, grid_y)
    inside = shapely.contains_xy(geometry, xx.ravel(), yy.ravel()).reshape(xx.shape)
    rows, columns = np.where(_medial_axis(inside))
    return np.column_stack([grid_x[columns], grid_y[rows]])


def hex_grid(bounds, pitch):
    """Hex-packed candidate centers covering the bounds, as an (N, 2) array."""
    min_x, min_y, max_x, max_y = bounds
    row_height = pitch * np.sqrt(3) / 2
    rows = np.arange(int(np.ceil((max_y - min_y) / row_height)) + 2)
    columns = np.arange(int(np.ceil((max_x - min_x) / pitch)) + 2)
    column_grid, row_grid = np.meshgrid(columns, rows)
    xs = min_x + column_grid * pitch + (row_grid % 2) * pitch / 2
    ys = min_y + row_grid * row_height
    return np.column_stack([xs.ravel(), ys.ravel()])


def _centerline_angles(centers, skeleton):
    """Direction of the nearby centerline at each center, in degrees."""
    angles = np.zeros(len(centers))
    if len(skeleton) < 2 or not len(centers):
        return angles
    neighbours = cKDTree(skeleton).query_ball_point(centers, TANGENT_SAMPLE_DISTANCE)
    for index, nearby in enumerate(neighbours):
        if len(nearby) < 2:
            continue
        offsets = skeleton[nearby] - skeleton[nearby].mean(axis=0)
        _, directions = np.linalg.eigh(offsets.T @ offsets)
        principal = directions[:, -1]
        angles[index] = np.degrees(np.arctan2(principal[1], principal[0]))
    return angles


def pyramid_footprints(positions, pyramid_size):
    """Square pyramid bases as polygons, matching rotate(45 + rotation) in the .scad."""
    corner_angles = np.radians(positions[:, 2, None] + 45 + np.array([0, 90, 180, 270]))
    corner_radius = pyramid_size / np.sqrt(2)
    corners = np.stack([
        positions[:, 0, None] + corner_radius * np.cos(corner_angles),
        positions[:, 1, None] + corner_radius * np.sin(corner_angles),
    ], axis=-1)
    return shapely.polygons(corners)


def pack_region(geometry, grid, settings):
    """Keep the grid positions whose whole pyramid base fits inside the geometry."""
    skeleton = calculate_skeleton(geometry, settings.skeleton_resolution)
    safe_area = geometry.buffer(-settings.safety_margin) if settings.safety_margin > 0 else geometry
    if safe_area.is_empty:
        return PackedRegion(np.empty((0, 3)), skeleton)

    centers = grid[shapely.contains_xy(safe_area, grid[:, 0], grid[:, 1])]
    angles = _centerline_angles(centers, skeleton) if settings.include_rotation else np.zeros(len(centers))
    candidates = np.column_stack([centers, angles])
    shapely.prepare(safe_area)
    fits = shapely.contains(safe_area, pyramid_footprints(candidates, settings.pyramid_size))
    return PackedRegion(candidates[fits], skeleton)


def pack_regions(regions, settings):
    """Pack every region on one shared grid so pyramid rows line up across the pad."""
    bounds = shapely.total_bounds([region.geometry for region in regions])
    grid = hex_grid(bounds, settings.pyramid_size + settings.pyramid_spacing)
    return {region.id: pack_region(region.geometry, grid, settings) for region in regions}
