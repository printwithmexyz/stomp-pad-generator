"""Square grid pack.

Lowest packing density of the four built-ins (~78.5% area vs 90.7% for
hex), but the regular row/column alignment reads visually as
"intentional" — good for letters, logos, and anywhere a deliberately
gridded look beats max density.
"""
from __future__ import annotations

import numpy as np

from . import register


class RectangularPattern:
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
        min_x, min_y, max_x, max_y = solid_geom.bounds
        num_rows = int(np.ceil((max_y - min_y) / pitch)) + 2
        num_cols = int(np.ceil((max_x - min_x) / pitch)) + 2

        out = []
        for row in range(num_rows):
            for col in range(num_cols):
                x = min_x + col * pitch
                y = min_y + row * pitch
                out.append([x, y, 0.0])
        return out


register("rectangular", RectangularPattern())
