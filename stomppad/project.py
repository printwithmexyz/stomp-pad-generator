"""ShapeProject — the cross-frontend contract.

A ``ShapeProject`` is the serializable state both the desktop and web
editors round-trip through: the components parsed from the SVG, the bodies
those components are grouped into, per-body color/pattern/overrides, and
the global pyramid parameters.

The model is intentionally JSON-first: :meth:`to_json` / :meth:`from_json`
are the only stable shape, and :data:`SCHEMA_VERSION` lets future phases
migrate older sidecar files (``<svg-stem>.project.json``) instead of
breaking them.

Phase 2.1 added a stateful selection/edit API on top of the v1 model:

- Selection lives **in the project** (``selected_component_ids``,
  ``selected_body_id``) so mutations can refer to "what the user has
  clicked" without the UI threading the set through every call. This is
  the deliberate choice that came out of the Phase 2 interview — a state
  machine is easier to reason about and test than a stateless surface
  where the UI carries selection between calls.
- Selection state is **ephemeral**: it is not persisted in
  :meth:`to_dict` / :meth:`to_json` and not loaded from sidecar files.
  Two builds of the same project may have different selection sets.
- Mutations emit events via the :meth:`on_change` observer hook so the
  per-body output cache (:mod:`stomppad.cache`) can invalidate selectively
  rather than recomputing the whole project on every click.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from shapely.geometry import Point, Polygon

from .geometry import Component


# Bump when from_json needs migration. Phase 1.4 shipped v1 — the first
# version users will see on disk via the Phase 2.3 sidecar convention.
SCHEMA_VERSION = 1

# Default pyramid pattern when no body-level override is set. Matches the
# Phase 0 behavior (skeleton-following hex packing).
DEFAULT_PATTERN = "skeleton"

# Filename suffix for sidecar JSON projects, attached to the SVG stem
# (``foo.svg`` → ``foo.project.json``). Batch mode auto-loads any sidecar
# it finds next to an input SVG (v2-plan, Resolved §3).
SIDECAR_SUFFIX = ".project.json"


# Event names emitted by ShapeProject._emit. Centralized as constants so
# callers can import them rather than typo-prone strings.
EVENT_SELECTION_CHANGED = "selection_changed"
EVENT_BODY_CHANGED = "body_changed"  # payload: body_ids=[...]
EVENT_TOPOLOGY_CHANGED = "topology_changed"  # add/remove components or bodies
EVENT_GLOBAL_PARAMS_CHANGED = "global_params_changed"

ChangeListener = Callable[[str, dict], None]


@dataclass
class Body:
    """A group of one or more Components that print as one colored region.

    ``component_ids`` references :class:`Component.id` values from the same
    :class:`ShapeProject`. ``color_hex`` defaults to the imported SVG fill
    of the largest-area component in the group (per the v2 plan's
    mixed-fill grouping rule). ``pattern`` is a registry key resolved by
    :mod:`stomppad.patterns`. ``pyramid_overrides`` carries per-body
    overrides (e.g. ``{"pyramid_size": 2.5}``) — empty means "use
    :attr:`ShapeProject.global_params`".
    """

    id: int
    name: str
    component_ids: list[int]
    color_hex: Optional[str] = None
    enabled: bool = True
    pattern: str = DEFAULT_PATTERN
    pyramid_overrides: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Body":
        return cls(
            id=d["id"],
            name=d["name"],
            component_ids=list(d.get("component_ids", [])),
            color_hex=d.get("color_hex"),
            enabled=d.get("enabled", True),
            pattern=d.get("pattern", DEFAULT_PATTERN),
            pyramid_overrides=dict(d.get("pyramid_overrides", {})),
        )


@dataclass
class ShapeProject:
    """The full editable state of one pad: components + bodies + params + selection.

    Selection lives on the project (see module docstring). Use the
    selection mutators (``select_component``, ``clear_selection`` …)
    rather than touching the private attributes directly so events fire
    and the cache stays consistent.
    """

    schema_version: int = SCHEMA_VERSION
    components: list[Component] = field(default_factory=list)
    bodies: list[Body] = field(default_factory=list)
    global_params: dict = field(default_factory=dict)
    svg_info: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Ephemeral state — not serialized in to_dict, not loaded from JSON.
        # Set here rather than via dataclass field so a dataclass.asdict()
        # / fields() walk excludes them automatically.
        self._selected_component_ids: set[int] = set()
        self._selected_body_id: Optional[int] = None
        self._listeners: list[ChangeListener] = []
        # Per-component polygon cache — building Polygon(exterior, holes=…)
        # is O(n) in the ring sizes, and hit_test reads it on every click.
        self._polygon_cache: dict[int, Polygon] = {}

    # ----------------------------------------------------------------- #
    # Factory + lookup
    # ----------------------------------------------------------------- #

    @classmethod
    def default_from_components(
        cls,
        components: list[Component],
        svg_info: dict,
        global_params: Optional[dict] = None,
    ) -> "ShapeProject":
        """Build a project with one body per top-level component.

        Defaults (per v2-plan "Resolved"):
          - each component → its own body
          - body color = the component's ``source_fill`` (single-component
            bodies make the "largest-area component wins" rule trivial)
          - default pattern = skeleton
          - all bodies enabled
        """
        bodies = [
            Body(
                id=i,
                name=f"body_{i}",
                component_ids=[comp.id],
                color_hex=comp.source_fill,
                enabled=True,
                pattern=DEFAULT_PATTERN,
                pyramid_overrides={},
            )
            for i, comp in enumerate(components)
        ]
        return cls(
            schema_version=SCHEMA_VERSION,
            components=list(components),
            bodies=bodies,
            global_params=dict(global_params or {}),
            svg_info=dict(svg_info),
        )

    def component(self, component_id: int) -> Component:
        for c in self.components:
            if c.id == component_id:
                return c
        raise KeyError(f"No component with id={component_id}")

    def component_or_none(self, component_id: int) -> Optional[Component]:
        for c in self.components:
            if c.id == component_id:
                return c
        return None

    def body(self, body_id: int) -> Body:
        for b in self.bodies:
            if b.id == body_id:
                return b
        raise KeyError(f"No body with id={body_id}")

    def body_or_none(self, body_id: int) -> Optional[Body]:
        for b in self.bodies:
            if b.id == body_id:
                return b
        return None

    def body_for_component(self, component_id: int) -> Optional[Body]:
        for b in self.bodies:
            if component_id in b.component_ids:
                return b
        return None

    # ----------------------------------------------------------------- #
    # Observer hook
    # ----------------------------------------------------------------- #

    def on_change(self, callback: ChangeListener) -> None:
        """Subscribe a callback. Called as ``callback(event_type, payload)``
        after every mutation. Used by :class:`stomppad.cache.BodyOutputCache`
        to invalidate selectively."""
        self._listeners.append(callback)

    def off_change(self, callback: ChangeListener) -> None:
        """Unsubscribe a callback previously registered via :meth:`on_change`.
        Silent on unknown callbacks; safe to call during teardown."""
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    def _emit(self, event_type: str, **payload) -> None:
        # Snapshot the list so a callback that calls off_change during
        # iteration doesn't mutate the list we're iterating over.
        for cb in list(self._listeners):
            cb(event_type, payload)

    # ----------------------------------------------------------------- #
    # Selection state (ephemeral)
    # ----------------------------------------------------------------- #

    @property
    def selected_component_ids(self) -> set[int]:
        # Return a copy so callers can't mutate the set without going through
        # the API (which is what fires events). Cheap for typical sizes.
        return set(self._selected_component_ids)

    @property
    def selected_body_id(self) -> Optional[int]:
        return self._selected_body_id

    def select_component(self, component_id: int) -> None:
        """Make ``component_id`` the sole selection (clears any previous)."""
        self.component(component_id)  # validates
        self._selected_component_ids = {component_id}
        self._emit(EVENT_SELECTION_CHANGED)

    def shift_select_component(self, component_id: int) -> None:
        """Toggle ``component_id`` into/out of the multi-select set."""
        self.component(component_id)
        if component_id in self._selected_component_ids:
            self._selected_component_ids.remove(component_id)
        else:
            self._selected_component_ids.add(component_id)
        self._emit(EVENT_SELECTION_CHANGED)

    def select_components(self, component_ids: Iterable[int]) -> None:
        """Replace the selection with the given set."""
        ids = set(component_ids)
        for cid in ids:
            self.component(cid)
        self._selected_component_ids = ids
        self._emit(EVENT_SELECTION_CHANGED)

    def clear_selection(self) -> None:
        if not self._selected_component_ids and self._selected_body_id is None:
            return
        self._selected_component_ids = set()
        self._selected_body_id = None
        self._emit(EVENT_SELECTION_CHANGED)

    def select_body(self, body_id: Optional[int]) -> None:
        """Highlight a body in the sidebar. ``None`` clears."""
        if body_id is not None:
            self.body(body_id)
        self._selected_body_id = body_id
        self._emit(EVENT_SELECTION_CHANGED)

    def _prune_stale_selection(self) -> None:
        """Drop selected ids that no longer exist after a structural mutation."""
        valid_components = {c.id for c in self.components}
        before = self._selected_component_ids
        self._selected_component_ids = before & valid_components
        valid_bodies = {b.id for b in self.bodies}
        if self._selected_body_id is not None and self._selected_body_id not in valid_bodies:
            self._selected_body_id = None
        # Caller already emits the topology event; selection drift is
        # implicit in that event for listeners that care.

    # ----------------------------------------------------------------- #
    # Body mutations (no topology change — same components, same bodies)
    # ----------------------------------------------------------------- #

    def toggle_body_enabled(self, body_id: int) -> None:
        body = self.body(body_id)
        body.enabled = not body.enabled
        self._emit(EVENT_BODY_CHANGED, body_ids=[body_id])

    def set_body_enabled(self, body_id: int, enabled: bool) -> None:
        body = self.body(body_id)
        if body.enabled == enabled:
            return
        body.enabled = enabled
        self._emit(EVENT_BODY_CHANGED, body_ids=[body_id])

    def set_body_color(self, body_id: int, color_hex: Optional[str]) -> None:
        body = self.body(body_id)
        if body.color_hex == color_hex:
            return
        body.color_hex = color_hex
        self._emit(EVENT_BODY_CHANGED, body_ids=[body_id])

    def set_body_pattern(self, body_id: int, pattern: str) -> None:
        body = self.body(body_id)
        if body.pattern == pattern:
            return
        body.pattern = pattern
        self._emit(EVENT_BODY_CHANGED, body_ids=[body_id])

    def set_body_pyramid_overrides(self, body_id: int, overrides: dict) -> None:
        body = self.body(body_id)
        new = dict(overrides)
        if body.pyramid_overrides == new:
            return
        body.pyramid_overrides = new
        self._emit(EVENT_BODY_CHANGED, body_ids=[body_id])

    def update_global_params(self, params: dict) -> None:
        """Merge ``params`` into ``global_params``. Empty merge = no-op."""
        if not params:
            return
        changed = False
        for k, v in params.items():
            if self.global_params.get(k) != v:
                self.global_params[k] = v
                changed = True
        if changed:
            self._emit(EVENT_GLOBAL_PARAMS_CHANGED)

    # ----------------------------------------------------------------- #
    # Body composition mutations (move components between bodies)
    # ----------------------------------------------------------------- #

    def _next_body_id(self) -> int:
        return max((b.id for b in self.bodies), default=-1) + 1

    def _next_component_id(self) -> int:
        return max((c.id for c in self.components), default=-1) + 1

    def _drop_empty_bodies(self) -> list[int]:
        """Remove bodies whose component_ids became empty. Return the dropped ids."""
        kept = []
        dropped = []
        for b in self.bodies:
            if b.component_ids:
                kept.append(b)
            else:
                dropped.append(b.id)
        self.bodies = kept
        return dropped

    def assign_components_to_body(
        self, component_ids: Iterable[int], body_id: int
    ) -> None:
        """Move ``component_ids`` into ``body_id``, removing them from any
        other body they currently belong to. Empty bodies are deleted.
        """
        ids = list(component_ids)
        target = self.body(body_id)
        for cid in ids:
            self.component(cid)  # validate
        for body in self.bodies:
            if body.id == body_id:
                continue
            body.component_ids = [c for c in body.component_ids if c not in ids]
        for cid in ids:
            if cid not in target.component_ids:
                target.component_ids.append(cid)
        dropped = self._drop_empty_bodies()
        affected = [body_id] + dropped
        # If a structural drop happened, treat it as topology too.
        if dropped:
            self._prune_stale_selection()
            self._emit(EVENT_TOPOLOGY_CHANGED, body_ids=affected)
        else:
            self._emit(EVENT_BODY_CHANGED, body_ids=affected)

    def group_selected_into_new_body(self, name: Optional[str] = None) -> int:
        """Create a fresh body wrapping the currently-selected components.
        Returns the new body's id. Raises if no components are selected.
        """
        if not self._selected_component_ids:
            raise ValueError("No components selected to group")
        new_id = self._next_body_id()
        ids_to_group = sorted(self._selected_component_ids)
        comps = [self.component(cid) for cid in ids_to_group]
        color = max(comps, key=lambda c: c.area).source_fill
        new_body = Body(
            id=new_id,
            name=name or f"body_{new_id}",
            component_ids=list(ids_to_group),
            color_hex=color,
            enabled=True,
            pattern=DEFAULT_PATTERN,
            pyramid_overrides={},
        )
        self.bodies.append(new_body)
        # Remove these components from the bodies they were in before.
        for body in self.bodies:
            if body.id == new_id:
                continue
            body.component_ids = [c for c in body.component_ids if c not in ids_to_group]
        dropped = self._drop_empty_bodies()
        self.clear_selection()
        self._emit(EVENT_TOPOLOGY_CHANGED, body_ids=[new_id] + dropped)
        return new_id

    # ----------------------------------------------------------------- #
    # Topology mutations: flip loop role between hole and body
    # ----------------------------------------------------------------- #

    def flip_hole_to_body(self, parent_component_id: int, hole_idx: int) -> int:
        """Promote the ``hole_idx``-th hole of ``parent_component_id`` into
        its own top-level component + body. Returns the new component id."""
        parent = self.component(parent_component_id)
        if not (0 <= hole_idx < len(parent.holes)):
            raise IndexError(
                f"Component {parent_component_id} has {len(parent.holes)} holes; "
                f"hole_idx {hole_idx} out of range."
            )
        hole_ring = parent.holes.pop(hole_idx)
        new_polygon = Polygon(hole_ring)
        new_id = self._next_component_id()
        new_component = Component(
            id=new_id,
            exterior=hole_ring,
            holes=[],
            bbox=new_polygon.bounds,
            area=new_polygon.area,
            source_fill=parent.source_fill,
        )
        self.components.append(new_component)
        body_id = self._next_body_id()
        self.bodies.append(
            Body(
                id=body_id,
                name=f"body_{body_id}",
                component_ids=[new_id],
                color_hex=parent.source_fill,
                enabled=True,
                pattern=DEFAULT_PATTERN,
                pyramid_overrides={},
            )
        )
        self._polygon_cache.pop(parent_component_id, None)
        self._emit(
            EVENT_TOPOLOGY_CHANGED,
            body_ids=[
                body_id,
                self.body_for_component(parent_component_id).id
                if self.body_for_component(parent_component_id)
                else -1,
            ],
        )
        return new_id

    def demote_body_to_hole(self, component_id: int) -> None:
        """Absorb ``component_id`` into the smallest enclosing component
        as a new hole. Removes the component and its body.

        The parent search uses each candidate's **exterior-only** polygon
        (ignoring its existing holes), so a depth-2 component sitting
        inside an ancestor's hole — the smiley-eye case — still finds the
        face as its parent. The newly-added hole may end up nested inside
        an existing hole; shapely accepts that and the
        point-in-polygon semantics for downstream pack steps stay correct
        (the eye region was already excluded from the face's interior).
        """
        comp = self.component(component_id)
        comp_polygon = self._polygon_for(component_id)
        pt = comp_polygon.representative_point()
        enclosing: Optional[Component] = None
        enclosing_area = float("inf")
        for other in self.components:
            if other.id == component_id or other.area <= comp.area:
                continue
            other_exterior = Polygon(other.exterior)
            if other_exterior.contains(pt) and other.area < enclosing_area:
                enclosing = other
                enclosing_area = other.area
        if enclosing is None:
            raise ValueError(
                f"Component {component_id} has no enclosing parent — "
                "cannot demote to hole (it's already top-level)."
            )
        enclosing.holes.append(list(comp.exterior))
        self.components = [c for c in self.components if c.id != component_id]
        body = self.body_for_component(component_id)
        if body is not None:
            body.component_ids = [c for c in body.component_ids if c != component_id]
        dropped = self._drop_empty_bodies()
        self._polygon_cache.pop(component_id, None)
        self._polygon_cache.pop(enclosing.id, None)
        self._prune_stale_selection()
        body_ids = ([body.id] if body else []) + dropped
        self._emit(EVENT_TOPOLOGY_CHANGED, body_ids=body_ids)

    # ----------------------------------------------------------------- #
    # Hit testing
    # ----------------------------------------------------------------- #

    def _polygon_for(self, component_id: int) -> Polygon:
        cached = self._polygon_cache.get(component_id)
        if cached is None:
            cached = self.component(component_id).to_polygon()
            self._polygon_cache[component_id] = cached
        return cached

    def hit_test(self, x: float, y: float) -> Optional[int]:
        """Return the component_id under ``(x, y)``, or ``None`` if the
        click misses (outside any component, or inside a component's hole).

        Iteration is smallest-area-first so that a depth-2 nested body
        (e.g. the smiley's eyes inside the face's hole) is reported in
        preference to any larger ancestor that geometrically contains
        the same point. ``Polygon(exterior, holes=holes).contains(pt)``
        naturally returns False for points inside a hole, which is the
        default behavior the Phase 2 interview locked in.
        """
        pt = Point(x, y)
        # Sort by area ascending so smaller (more specific) components win
        # ties. Tie example: a depth-2 body sits geometrically inside the
        # depth-0 ancestor's hole; the ancestor's polygon-with-hole returns
        # False at the depth-2's location, but defensive sorting still
        # protects against pathological overlaps from malformed SVGs.
        for component in sorted(self.components, key=lambda c: c.area):
            poly = self._polygon_for(component.id)
            if poly.contains(pt):
                return component.id
        return None

    # ----------------------------------------------------------------- #
    # JSON (de)serialization
    # ----------------------------------------------------------------- #

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "components": [c.to_dict() for c in self.components],
            "bodies": [b.to_dict() for b in self.bodies],
            "global_params": self.global_params,
            "svg_info": self.svg_info,
        }

    def to_json(self, indent: Optional[int] = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def to_editor_dict(self, positions_by_body: Optional[dict] = None) -> dict:
        """Snapshot for the JS editor: project state + ephemeral selection +
        (optional) cached pack positions, in one Pyodide round-trip.

        Selection IS included here (the editor needs it to draw highlights)
        but is intentionally absent from :meth:`to_dict` so the sidecar
        JSON never picks it up.
        """
        out = self.to_dict()
        out["selected_component_ids"] = sorted(self._selected_component_ids)
        out["selected_body_id"] = self._selected_body_id
        if positions_by_body is not None:
            out["positions_by_body"] = {
                str(bid): positions for bid, positions in positions_by_body.items()
            }
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "ShapeProject":
        version = d.get("schema_version")
        if version is None:
            raise ValueError(
                "ShapeProject JSON missing required 'schema_version' field."
            )
        if version > SCHEMA_VERSION:
            raise ValueError(
                f"ShapeProject schema_version {version} is newer than this "
                f"build supports (max {SCHEMA_VERSION}). Upgrade stomppad."
            )
        if version < 1:
            raise ValueError(
                f"ShapeProject schema_version {version} is below the minimum "
                f"supported (1). The field exists from day one — anything "
                f"older is corrupt."
            )
        # Future Phase 2/3 versions branch here and migrate old shapes forward
        # before reconstructing the dataclass.
        return cls(
            schema_version=version,
            components=[Component.from_dict(c) for c in d.get("components", [])],
            bodies=[Body.from_dict(b) for b in d.get("bodies", [])],
            global_params=dict(d.get("global_params", {})),
            svg_info=dict(d.get("svg_info", {})),
        )

    @classmethod
    def from_json(cls, s: str) -> "ShapeProject":
        return cls.from_dict(json.loads(s))

    # ----------------------------------------------------------------- #
    # Sidecar JSON: ``<svg-stem>.project.json`` next to the source SVG
    # ----------------------------------------------------------------- #

    @staticmethod
    def sidecar_path_for(svg_path) -> Path:
        """Conventional sidecar location: ``foo.svg`` → ``foo.project.json``."""
        svg_path = Path(svg_path)
        return svg_path.with_name(svg_path.stem + SIDECAR_SUFFIX)

    def save_sidecar(self, svg_path, logger=None) -> Optional[Path]:
        """Write this project to ``foo.project.json`` next to ``svg_path``.

        Atomic: writes to a sibling ``.tmp`` and then ``replace()``s into
        position. POSIX gives a real atomic rename; on Windows NTFS the
        rename is transactional in the common case (not strictly atomic on
        crash, but far safer than an in-place truncate-and-rewrite). The
        Phase 2 auto-save loop fires every 500 ms, so the corruption
        window for the in-place pattern was wide.

        On Windows ``Path.replace`` raises ``PermissionError`` when the
        destination is currently open in another process (a slicer holding
        the sidecar, for instance). Auto-save would otherwise crash the
        editor every tick. We catch + log + skip, leaving the temp file
        in place for the next tick to retry; the caller gets ``None``.
        """
        sidecar = self.sidecar_path_for(svg_path)
        tmp = sidecar.with_name(sidecar.name + ".tmp")
        try:
            tmp.write_text(self.to_json(), encoding="utf-8")
            tmp.replace(sidecar)
        except PermissionError as exc:
            if logger is not None:
                logger(
                    f"save_sidecar: skipped — {sidecar.name} is locked "
                    f"({exc}). Retry on next auto-save tick."
                )
            return None
        return sidecar

    @classmethod
    def load_sidecar(cls, svg_path) -> Optional["ShapeProject"]:
        """Load and return the sidecar project for ``svg_path``, or ``None``
        if the sidecar doesn't exist. JSON parse / schema errors propagate
        as :class:`ValueError` from :meth:`from_dict`.
        """
        sidecar = cls.sidecar_path_for(svg_path)
        if not sidecar.exists():
            return None
        return cls.from_json(sidecar.read_text(encoding="utf-8"))
