"""Triangular lattice (60° offset rows).

Same vertex density as hexagonal but with the alternate-row offset based
on full pitch rather than half-pitch — produces equilateral triangles of
pyramid centers. Visually distinct from hex; useful where the rotated
diamond bases would otherwise read as squares.
"""
from __future__ import annotations

import numpy as np

from . import register


class TriangularPattern:
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
        # Equilateral triangle row spacing: side * sqrt(3)/2.
        # Same as hex on Y; alternating rows shift by pitch/2 in X but
        # **also** halve column count on every other row would gap too
        # much — keep all columns, just shift, matching the standard
        # triangular lattice with shared vertices on each row.
        row_y = pitch * np.sqrt(3) / 2

        min_x, min_y, max_x, max_y = solid_geom.bounds
        num_rows = int(np.ceil((max_y - min_y) / row_y)) + 2
        num_cols = int(np.ceil((max_x - min_x) / pitch)) + 2

        out = []
        for row in range(num_rows):
            shift = (row % 2) * (pitch / 2)
            for col in range(num_cols):
                x = min_x + col * pitch + shift
                y = min_y + row * row_y
                out.append([x, y, 0.0])
        return out


register("triangular", TriangularPattern())
