"""Per-body output cache for the Phase 2 editor.

The Phase 2 plan call: "re-pack only edited bodies." Without a cache,
every selection click or color change in the sidebar would re-pack the
entire :class:`stomppad.project.ShapeProject` (skeleton raster + hex
candidate grid + N^2 contains tests per body). For multi-body files
that's wall-clock seconds per click — unusable in an interactive editor.

:class:`BodyOutputCache` solves this by:

1. Lazily packing each body the first time its positions are requested.
2. Memoizing positions per ``body.id``.
3. Subscribing to the project's :meth:`on_change` events; invalidating
   selectively when one body changes, and clearing the whole cache when
   topology or global params change.
4. Holding a long-lived :class:`stomppad.packing.PackContext` so the
   per-component skeleton cache (the slow step in Pyodide) also survives
   across body edits.

The cache is intentionally **stateless w.r.t. selection**: clicking a
component to highlight it doesn't invalidate anything. Only body content
or topology changes do.
"""
from __future__ import annotations

from typing import Optional

from .packing import PackContext, pack_component
from .project import (
    EVENT_BODY_CHANGED,
    EVENT_GLOBAL_PARAMS_CHANGED,
    EVENT_TOPOLOGY_CHANGED,
    Body,
    ShapeProject,
)


# Default pack params used when neither the project's ``global_params`` nor
# the body's ``pyramid_overrides`` set a value. Matches the defaults the
# desktop GUI's parameter form ships with so existing batch behavior is
# preserved when no edits have been made.
DEFAULT_PACK_PARAMS = {
    "pyramid_size": 4,
    "pyramid_spacing": 1,
    "safety_margin": 0.5,
    "include_rotation": True,
    "skeleton_resolution": 0.3,
}


def _merged_pack_params(project: ShapeProject, body: Body) -> dict:
    """Merge defaults < global_params < body.pyramid_overrides."""
    merged = dict(DEFAULT_PACK_PARAMS)
    merged.update(project.global_params)
    merged.update(body.pyramid_overrides)
    return merged


class BodyOutputCache:
    """Memoized per-body packing scoped to one :class:`ShapeProject`.

    Construct one cache per project. The cache subscribes to the
    project's change events on construction, so creating a fresh project
    on file-open should also create a fresh cache rather than reusing an
    old one (otherwise the old listener leaks).
    """

    def __init__(
        self,
        project: ShapeProject,
        pack_context: Optional[PackContext] = None,
    ) -> None:
        self._project = project
        self._pack_context = pack_context or PackContext()
        self._positions: dict[int, list[list[float]]] = {}
        project.on_change(self._on_project_change)

    # ----------------------------------------------------------------- #
    # Read API
    # ----------------------------------------------------------------- #

    def positions_for(self, body_id: int, *, logger=None) -> list[list[float]]:
        """Return cached positions for ``body_id``, packing on first miss.

        Disabled bodies always return ``[]`` without touching the cache —
        future enable-toggles will still trigger a fresh pack.
        """
        body = self._project.body(body_id)
        if not body.enabled:
            return []
        cached = self._positions.get(body_id)
        if cached is not None:
            return cached
        positions = self._compute_positions(body, logger=logger)
        self._positions[body_id] = positions
        return positions

    def all_positions(self, *, logger=None) -> dict[int, list[list[float]]]:
        """Pack every enabled body and return ``{body_id: positions}``."""
        out: dict[int, list[list[float]]] = {}
        for body in self._project.bodies:
            if body.enabled:
                out[body.id] = self.positions_for(body.id, logger=logger)
        return out

    # ----------------------------------------------------------------- #
    # Invalidation
    # ----------------------------------------------------------------- #

    def invalidate_body(self, body_id: int) -> None:
        """Drop cached positions for ``body_id`` and the per-component
        skeletons under it (so the next pack rebuilds them)."""
        self._positions.pop(body_id, None)
        body = self._project.body_or_none(body_id)
        if body:
            for cid in body.component_ids:
                self._pack_context.invalidate(cid)

    def invalidate_all(self) -> None:
        """Clear all per-body positions and all per-component skeletons."""
        self._positions.clear()
        self._pack_context.clear()

    # ----------------------------------------------------------------- #
    # Internals
    # ----------------------------------------------------------------- #

    def _on_project_change(self, event_type: str, payload: dict) -> None:
        if event_type == EVENT_BODY_CHANGED:
            for bid in payload.get("body_ids", []):
                self.invalidate_body(bid)
        elif event_type in (EVENT_TOPOLOGY_CHANGED, EVENT_GLOBAL_PARAMS_CHANGED):
            self.invalidate_all()
        # EVENT_SELECTION_CHANGED is intentionally ignored.

    def _compute_positions(self, body: Body, *, logger=None) -> list[list[float]]:
        params = _merged_pack_params(self._project, body)
        positions: list[list[float]] = []
        for cid in body.component_ids:
            component = self._project.component_or_none(cid)
            if component is None:
                # Stale id (e.g. mid-mutation). Skip rather than crash.
                continue
            positions.extend(
                pack_component(
                    component,
                    pyramid_size=params["pyramid_size"],
                    pyramid_spacing=params["pyramid_spacing"],
                    safety_margin=params["safety_margin"],
                    include_rotation=params["include_rotation"],
                    skeleton_resolution=params["skeleton_resolution"],
                    context=self._pack_context,
                    pattern=body.pattern,
                    logger=logger,
                )
            )
        return positions
