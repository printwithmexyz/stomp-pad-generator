"""Uniform hexagonal grid (no skeleton).

Same lattice as :mod:`stomppad.patterns.skeleton` minus the per-position
tangent rotation, so every pyramid sits at 0°. Useful for rounded shapes
where skeleton alignment is meaningless (a perfect disk's medial axis is
a single point, so the skeleton pattern degenerates to all-zero rotation
anyway — this skips the raster cost entirely).
"""
from __future__ import annotations

import numpy as np

from . import register


class HexagonalPattern:
    needs_skeleton: bool = False

    def generate_positions(
        self,
        *,
        solid_geom,
        pyramid_size: float,
        pyramid_spacing: float,
        include_rotation: bool,
        skeleton_points,
        logger=None,
    ) -> list[list[float]]:
        pitch = pyramid_size + pyramid_spacing
        hex_x = pitch
        hex_y = pitch * np.sqrt(3) / 2

        min_x, min_y, max_x, max_y = solid_geom.bounds
        num_rows = int(np.ceil((max_y - min_y) / hex_y)) + 2
        num_cols = int(np.ceil((max_x - min_x) / hex_x)) + 2

        out = []
        for row in range(num_rows):
            x_offset = (row % 2) * (hex_x / 2)
            for col in range(num_cols):
                x = min_x + col * hex_x + x_offset
                y = min_y + row * hex_y
                out.append([x, y, 0.0])
        return out


register("hexagonal", HexagonalPattern())
