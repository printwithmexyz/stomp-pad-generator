"""Phase 2.1 acceptance tests.

Covers the v2-plan Phase 2.1 contract:
- Stateful selection (single / multi / shift-toggle / clear)
- Body composition mutations (assign, group_selected_into_new_body)
- Body property mutations (color, pattern, enabled, overrides)
- Topology mutations (flip_hole_to_body, demote_body_to_hole)
- hit_test (component / hole / miss / nested body inside a hole)
- BodyOutputCache memoization + selective invalidation
- Sidecar JSON round-trip

UI-side concerns (the web sidebar, the desktop Edit tab) live in their
own test layers (the web has no Python tests; the desktop's sidecar
load/save is tested here through ShapeProject directly).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from stomppad import (  # noqa: E402
    Body,
    BodyOutputCache,
    Component,
    EVENT_BODY_CHANGED,
    EVENT_GLOBAL_PARAMS_CHANGED,
    EVENT_SELECTION_CHANGED,
    EVENT_TOPOLOGY_CHANGED,
    ShapeProject,
    parse_svg_to_components,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

SMILEY = """<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect x="0" y="0" width="100" height="100" fill="#ffffff"/>
  <circle cx="50" cy="50" r="40" fill="#010102"/>
  <circle cx="50" cy="50" r="30" fill="#ffffff"/>
  <circle cx="40" cy="45" r="3" fill="#010102"/>
  <circle cx="60" cy="45" r="3" fill="#010102"/>
</svg>"""

DISJOINT = """<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect x="10" y="10" width="20" height="20" fill="#ff0000"/>
  <rect x="40" y="10" width="20" height="20" fill="#00ff00"/>
  <rect x="70" y="10" width="20" height="20" fill="#0000ff"/>
</svg>"""

DONUT = """<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect x="20" y="20" width="60" height="60" fill="#ff0000"/>
  <rect x="40" y="40" width="20" height="20" fill="#ffffff"/>
</svg>"""


def _project_from_svg(tmp_path: Path, name: str, body: str) -> ShapeProject:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    components, info = parse_svg_to_components(str(p), target_width=80)
    return ShapeProject.default_from_components(components, info)


class _EventRecorder:
    """Subscribe and record events for assertion."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))

    def types(self) -> list[str]:
        return [t for t, _ in self.events]


# --------------------------------------------------------------------------- #
# Selection state
# --------------------------------------------------------------------------- #


def test_select_component_replaces_previous(tmp_path):
    project = _project_from_svg(tmp_path, "disjoint.svg", DISJOINT)
    recorder = _EventRecorder()
    project.on_change(recorder)
    project.select_component(0)
    project.select_component(2)
    assert project.selected_component_ids == {2}
    assert recorder.types() == [EVENT_SELECTION_CHANGED, EVENT_SELECTION_CHANGED]


def test_shift_select_toggles_in_and_out(tmp_path):
    project = _project_from_svg(tmp_path, "disjoint.svg", DISJOINT)
    project.shift_select_component(0)
    project.shift_select_component(1)
    project.shift_select_component(2)
    assert project.selected_component_ids == {0, 1, 2}
    project.shift_select_component(1)
    assert project.selected_component_ids == {0, 2}


def test_clear_selection_no_op_when_empty(tmp_path):
    project = _project_from_svg(tmp_path, "disjoint.svg", DISJOINT)
    recorder = _EventRecorder()
    project.on_change(recorder)
    project.clear_selection()
    assert recorder.events == []


def test_select_unknown_component_raises(tmp_path):
    project = _project_from_svg(tmp_path, "disjoint.svg", DISJOINT)
    with pytest.raises(KeyError):
        project.select_component(99)


# --------------------------------------------------------------------------- #
# hit_test
# --------------------------------------------------------------------------- #


def test_hit_test_inside_a_component_returns_its_id(tmp_path):
    # Disjoint test: scaled rects map to known regions; click the middle one.
    project = _project_from_svg(tmp_path, "disjoint.svg", DISJOINT)
    middle = [c for c in project.components if c.source_fill == "#00ff00"][0]
    cx = (middle.bbox[0] + middle.bbox[2]) / 2
    cy = (middle.bbox[1] + middle.bbox[3]) / 2
    assert project.hit_test(cx, cy) == middle.id


def test_hit_test_in_a_hole_returns_none(tmp_path):
    project = _project_from_svg(tmp_path, "donut.svg", DONUT)
    component = project.components[0]
    hole = component.holes[0]
    hx = sum(p[0] for p in hole) / len(hole)
    hy = sum(p[1] for p in hole) / len(hole)
    # The hole's geometric center should be inside the hole — clicks there
    # belong to no body per the locked default (a). The parent doesn't
    # "win" by containment alone.
    assert project.hit_test(hx, hy) is None


def test_hit_test_outside_geometry_returns_none(tmp_path):
    project = _project_from_svg(tmp_path, "disjoint.svg", DISJOINT)
    assert project.hit_test(-1000, -1000) is None


def test_hit_test_picks_nested_body_inside_a_hole(tmp_path):
    # The smiley's eyes are depth-2 components sitting geometrically
    # inside the face's hole — hit_test must pick the eye, not return
    # None (the eye component "wins" over the face's hole exclusion).
    project = _project_from_svg(tmp_path, "smiley.svg", SMILEY)
    eyes = [c for c in project.components if c.area < 100]
    assert len(eyes) == 2
    eye = eyes[0]
    cx = (eye.bbox[0] + eye.bbox[2]) / 2
    cy = (eye.bbox[1] + eye.bbox[3]) / 2
    assert project.hit_test(cx, cy) == eye.id


# --------------------------------------------------------------------------- #
# Body composition mutations
# --------------------------------------------------------------------------- #


def test_assign_components_to_body_moves_them(tmp_path):
    project = _project_from_svg(tmp_path, "disjoint.svg", DISJOINT)
    # Move component 0 from its body into body 1 (which holds component 1).
    project.assign_components_to_body([0], 1)
    assert sorted(project.body(1).component_ids) == [0, 1]
    # Body 0 should be gone (empty after losing its only component).
    assert project.body_or_none(0) is None


def test_group_selected_into_new_body(tmp_path):
    project = _project_from_svg(tmp_path, "disjoint.svg", DISJOINT)
    project.shift_select_component(0)
    project.shift_select_component(2)
    new_id = project.group_selected_into_new_body(name="ends")
    body = project.body(new_id)
    assert sorted(body.component_ids) == [0, 2]
    assert body.name == "ends"
    # Grouping clears selection so the next click starts fresh.
    assert project.selected_component_ids == set()


def test_group_with_no_selection_raises(tmp_path):
    project = _project_from_svg(tmp_path, "disjoint.svg", DISJOINT)
    with pytest.raises(ValueError):
        project.group_selected_into_new_body()


# --------------------------------------------------------------------------- #
# Body property mutations + event payloads
# --------------------------------------------------------------------------- #


def test_set_body_color_emits_body_changed(tmp_path):
    project = _project_from_svg(tmp_path, "disjoint.svg", DISJOINT)
    recorder = _EventRecorder()
    project.on_change(recorder)
    project.set_body_color(1, "#abcdef")
    assert project.body(1).color_hex == "#abcdef"
    assert recorder.events == [(EVENT_BODY_CHANGED, {"body_ids": [1]})]


def test_set_body_pattern_unchanged_is_silent(tmp_path):
    project = _project_from_svg(tmp_path, "disjoint.svg", DISJOINT)
    recorder = _EventRecorder()
    project.on_change(recorder)
    current = project.body(0).pattern
    project.set_body_pattern(0, current)
    assert recorder.events == []


def test_toggle_body_enabled_flips(tmp_path):
    project = _project_from_svg(tmp_path, "disjoint.svg", DISJOINT)
    assert project.body(0).enabled is True
    project.toggle_body_enabled(0)
    assert project.body(0).enabled is False
    project.toggle_body_enabled(0)
    assert project.body(0).enabled is True


def test_set_body_pyramid_overrides(tmp_path):
    project = _project_from_svg(tmp_path, "disjoint.svg", DISJOINT)
    project.set_body_pyramid_overrides(0, {"pyramid_size": 2.5})
    assert project.body(0).pyramid_overrides == {"pyramid_size": 2.5}


def test_update_global_params_merges_not_replaces(tmp_path):
    project = _project_from_svg(tmp_path, "disjoint.svg", DISJOINT)
    project.global_params["pyramid_size"] = 3
    project.update_global_params({"pyramid_spacing": 1.5})
    assert project.global_params == {"pyramid_size": 3, "pyramid_spacing": 1.5}


# --------------------------------------------------------------------------- #
# Topology mutations
# --------------------------------------------------------------------------- #


def test_flip_hole_to_body_creates_component_and_body(tmp_path):
    project = _project_from_svg(tmp_path, "donut.svg", DONUT)
    parent = project.components[0]
    parent_id = parent.id
    before_components = len(project.components)
    before_bodies = len(project.bodies)
    new_cid = project.flip_hole_to_body(parent_id, 0)
    assert len(project.components) == before_components + 1
    assert len(project.bodies) == before_bodies + 1
    # Parent loses its hole.
    assert project.component(parent_id).holes == []
    new_component = project.component(new_cid)
    assert new_component.area > 0


def test_demote_body_to_hole_absorbs_into_parent(tmp_path):
    # Use the smiley — eyes are good candidates: depth-2 bodies that sit
    # inside the face's hole. Demoting an eye should absorb it as a new
    # hole of the face component.
    project = _project_from_svg(tmp_path, "smiley.svg", SMILEY)
    eyes = sorted(
        (c for c in project.components if c.area < 100), key=lambda c: c.id
    )
    eye_id = eyes[0].id
    face = max(project.components, key=lambda c: c.area)
    face_holes_before = len(face.holes)
    project.demote_body_to_hole(eye_id)
    assert project.component_or_none(eye_id) is None
    assert len(face.holes) == face_holes_before + 1


def test_demote_top_level_component_raises(tmp_path):
    # Disjoint rects: none of them are enclosed by another, so demoting
    # any one of them is invalid (no parent to absorb into).
    project = _project_from_svg(tmp_path, "disjoint.svg", DISJOINT)
    with pytest.raises(ValueError):
        project.demote_body_to_hole(project.components[0].id)


def test_topology_event_includes_affected_body_ids(tmp_path):
    project = _project_from_svg(tmp_path, "donut.svg", DONUT)
    recorder = _EventRecorder()
    project.on_change(recorder)
    project.flip_hole_to_body(project.components[0].id, 0)
    assert any(t == EVENT_TOPOLOGY_CHANGED for t, _ in recorder.events)


# --------------------------------------------------------------------------- #
# BodyOutputCache
# --------------------------------------------------------------------------- #


def test_cache_returns_same_object_on_hit(tmp_path):
    project = _project_from_svg(tmp_path, "disjoint.svg", DISJOINT)
    cache = BodyOutputCache(project)
    a = cache.positions_for(0)
    b = cache.positions_for(0)
    assert a is b


def test_cache_invalidates_only_changed_body(tmp_path):
    project = _project_from_svg(tmp_path, "disjoint.svg", DISJOINT)
    cache = BodyOutputCache(project)
    a0 = cache.positions_for(0)
    a1 = cache.positions_for(1)
    project.set_body_pattern(0, "rectangular")
    b0 = cache.positions_for(0)
    b1 = cache.positions_for(1)
    assert b0 is not a0, "body 0 cache should have been invalidated"
    assert b1 is a1, "body 1 was untouched and should remain cached"


def test_cache_clears_on_topology_change(tmp_path):
    project = _project_from_svg(tmp_path, "donut.svg", DONUT)
    cache = BodyOutputCache(project)
    a = cache.positions_for(0)
    project.flip_hole_to_body(project.components[0].id, 0)
    # Cache must be cleared; positions_for(0) now packs the post-flip shape.
    b = cache.positions_for(0)
    assert b is not a


def test_cache_clears_on_global_params_change(tmp_path):
    project = _project_from_svg(tmp_path, "disjoint.svg", DISJOINT)
    cache = BodyOutputCache(project)
    a0 = cache.positions_for(0)
    project.update_global_params({"pyramid_size": 2})
    b0 = cache.positions_for(0)
    assert b0 is not a0


def test_cache_disabled_body_returns_empty(tmp_path):
    project = _project_from_svg(tmp_path, "disjoint.svg", DISJOINT)
    cache = BodyOutputCache(project)
    project.set_body_enabled(0, False)
    assert cache.positions_for(0) == []


def test_cache_selection_change_does_not_invalidate(tmp_path):
    project = _project_from_svg(tmp_path, "disjoint.svg", DISJOINT)
    cache = BodyOutputCache(project)
    a = cache.positions_for(0)
    project.select_component(1)
    b = cache.positions_for(0)
    assert b is a, "selection changes must not invalidate body caches"


def test_off_change_unsubscribes(tmp_path):
    project = _project_from_svg(tmp_path, "disjoint.svg", DISJOINT)
    recorder = _EventRecorder()
    project.on_change(recorder)
    project.set_body_color(0, "#aabbcc")
    project.off_change(recorder)
    project.set_body_color(0, "#112233")
    assert recorder.types() == [EVENT_BODY_CHANGED]


def test_off_change_safe_to_remove_during_emit(tmp_path):
    """A listener that unsubscribes mid-emit must not crash the iteration."""
    project = _project_from_svg(tmp_path, "disjoint.svg", DISJOINT)
    fired = []

    def self_removing(event_type, payload):
        fired.append(event_type)
        project.off_change(self_removing)

    project.on_change(self_removing)
    project.set_body_color(0, "#aabbcc")
    project.set_body_color(0, "#112233")
    assert fired == [EVENT_BODY_CHANGED]


# --------------------------------------------------------------------------- #
# Sidecar JSON
# --------------------------------------------------------------------------- #


def test_sidecar_path_convention(tmp_path):
    p = tmp_path / "foo.svg"
    expected = tmp_path / "foo.project.json"
    assert ShapeProject.sidecar_path_for(p) == expected


def test_save_then_load_sidecar_round_trip(tmp_path):
    svg_path = tmp_path / "disjoint.svg"
    svg_path.write_text(DISJOINT, encoding="utf-8")
    components, info = parse_svg_to_components(str(svg_path), target_width=80)
    project = ShapeProject.default_from_components(components, info)
    project.set_body_color(0, "#abcdef")
    project.set_body_pattern(1, "hexagonal")
    sidecar = project.save_sidecar(svg_path)
    assert sidecar.exists()
    loaded = ShapeProject.load_sidecar(svg_path)
    assert loaded is not None
    assert loaded.body(0).color_hex == "#abcdef"
    assert loaded.body(1).pattern == "hexagonal"
    # Ephemeral state must NOT round-trip — selection always starts empty.
    assert loaded.selected_component_ids == set()


def test_load_sidecar_missing_returns_none(tmp_path):
    svg_path = tmp_path / "nope.svg"
    svg_path.write_text(DISJOINT, encoding="utf-8")
    assert ShapeProject.load_sidecar(svg_path) is None


# --------------------------------------------------------------------------- #
# Selection ephemerality
# --------------------------------------------------------------------------- #


def test_selection_is_not_serialized(tmp_path):
    project = _project_from_svg(tmp_path, "disjoint.svg", DISJOINT)
    project.select_component(0)
    project.select_body(1)
    d = project.to_dict()
    assert "selected_component_ids" not in d
    assert "selected_body_id" not in d


def test_topology_change_prunes_selection(tmp_path):
    project = _project_from_svg(tmp_path, "donut.svg", DONUT)
    project.shift_select_component(project.components[0].id)
    project.flip_hole_to_body(project.components[0].id, 0)
    # The selected id might still be valid (parent stuck around), but
    # demoting a selected component is the case that exercises the prune.
    project = _project_from_svg(tmp_path, "smiley.svg", SMILEY)
    eye = sorted(
        (c for c in project.components if c.area < 100), key=lambda c: c.id
    )[0]
    project.shift_select_component(eye.id)
    assert eye.id in project.selected_component_ids
    project.demote_body_to_hole(eye.id)
    assert eye.id not in project.selected_component_ids
