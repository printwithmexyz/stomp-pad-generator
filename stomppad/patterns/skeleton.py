"""Skeleton-following hex pack.

The Phase 0 behavior: generate a hex grid covering the bounding box, then
rotate each pyramid to align with the local centerline tangent so they
"flow" along the shape's spine (the long axis of melt-drip blobs, the
arc of a letter stroke, etc.).

Lives in the pattern registry so Phase 2's editor can swap it per-body.
"""
from __future__ import annotations

import numpy as np

from . import register
from ..packing import calculate_centerline_tangent


class SkeletonPattern:
    """Hexagonal grid with rotation aligned to medial-axis tangent."""

    needs_skeleton: bool = True

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
        if not skeleton_points:
            if logger:
                logger("  WARNING: SkeletonPattern called with empty skeleton")
            return []

        pyramid_pitch = pyramid_size + pyramid_spacing
        hex_x = pyramid_pitch
        hex_y = pyramid_pitch * np.sqrt(3) / 2

        min_x, min_y, max_x, max_y = solid_geom.bounds
        num_rows = int(np.ceil((max_y - min_y) / hex_y)) + 2
        num_cols = int(np.ceil((max_x - min_x) / hex_x)) + 2

        out = []
        for row in range(num_rows):
            x_offset = (row % 2) * (hex_x / 2)
            for col in range(num_cols):
                x = min_x + col * hex_x + x_offset
                y = min_y + row * hex_y
                rot = (
                    calculate_centerline_tangent(skeleton_points, x, y)
                    if include_rotation
                    else 0.0
                )
                out.append([x, y, rot])
        return out


register("skeleton", SkeletonPattern())
