"""Phase 1 acceptance tests.

Covers the assertions called out in docs/v2-plan.md Phase 1.5:
- Component & hole counts on representative SVG shapes
- Background ring auto-drop
- Zero pyramid centers land inside any hole
- Each pattern produces non-trivial output on a known rectangle
- ShapeProject JSON round-trips

The Phase 0 byte-equality regression lives separately in
``tests/test_regression.py`` so a Phase 1 acceptance failure doesn't mask
a single-shape drift (the two are orthogonal guards).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from shapely.geometry import Point, Polygon

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from stomppad import (  # noqa: E402
    Body,
    Component,
    PackContext,
    ShapeProject,
    pack_component,
    parse_svg_to_components,
    patterns,
)


# --------------------------------------------------------------------------- #
# Fixtures — synthetic SVGs as inline strings so the test stays self-contained
# (and so the v2-plan "real smiley" can be dropped in later as a single file
# without forcing reorganization).
# --------------------------------------------------------------------------- #


def _write_svg(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


DONUT = """<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect x="20" y="20" width="60" height="60" fill="#ff0000"/>
  <rect x="40" y="40" width="20" height="20" fill="#ffffff"/>
</svg>"""

DISJOINT = """<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect x="10" y="10" width="20" height="20" fill="#ff0000"/>
  <rect x="40" y="10" width="20" height="20" fill="#00ff00"/>
  <rect x="70" y="10" width="20" height="20" fill="#0000ff"/>
</svg>"""

TWO_HOLE = """<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect x="10" y="10" width="80" height="80" fill="#000000"/>
  <rect x="20" y="20" width="20" height="20" fill="#ffffff"/>
  <rect x="60" y="60" width="20" height="20" fill="#ffffff"/>
</svg>"""

NESTED = """<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect x="10" y="10" width="80" height="80" fill="#000000"/>
  <rect x="20" y="20" width="60" height="60" fill="#ffffff"/>
  <rect x="30" y="30" width="40" height="40" fill="#ff0000"/>
  <rect x="40" y="40" width="20" height="20" fill="#00ff00"/>
</svg>"""

# Smiley-shaped: full-bleed background rect, outer face disk, inner white
# disk (= the hole), two black eyes inside the white area.
SMILEY = """<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect x="0" y="0" width="100" height="100" fill="#ffffff"/>
  <circle cx="50" cy="50" r="40" fill="#010102"/>
  <circle cx="50" cy="50" r="30" fill="#ffffff"/>
  <circle cx="40" cy="45" r="3" fill="#010102"/>
  <circle cx="60" cy="45" r="3" fill="#010102"/>
</svg>"""

# Full-bleed badge: one large near-canvas rect that must NOT be dropped
# (it's the only content; dropping it leaves nothing to pack).
FULL_BLEED_BADGE = """<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect x="0" y="0" width="100" height="100" fill="#ff0000"/>
</svg>"""


# --------------------------------------------------------------------------- #
# Component parsing
# --------------------------------------------------------------------------- #


def test_donut_yields_one_component_with_one_hole(tmp_path):
    svg = _write_svg(tmp_path, "donut.svg", DONUT)
    components, info = parse_svg_to_components(str(svg), target_width=60)
    assert len(components) == 1
    assert len(components[0].holes) == 1
    assert info["background_dropped"] == 0


def test_disjoint_yields_three_components(tmp_path):
    svg = _write_svg(tmp_path, "disjoint.svg", DISJOINT)
    components, info = parse_svg_to_components(str(svg), target_width=80)
    assert len(components) == 3
    assert all(len(c.holes) == 0 for c in components)
    # Source fills survive parsing — Phase 2 will use these as body color defaults.
    fills = sorted(c.source_fill for c in components)
    assert fills == ["#0000ff", "#00ff00", "#ff0000"]


def test_two_hole_keeps_both_holes(tmp_path):
    svg = _write_svg(tmp_path, "two_hole.svg", TWO_HOLE)
    components, info = parse_svg_to_components(str(svg), target_width=80)
    assert len(components) == 1
    assert len(components[0].holes) == 2


def test_nested_alternates_body_hole_body_hole(tmp_path):
    # Depth 0 (outer black) → body, depth 1 (white) → hole of outer,
    # depth 2 (red) → new body inside the hole, depth 3 (green) → hole of red.
    # End result: 2 components, each with 1 hole.
    svg = _write_svg(tmp_path, "nested.svg", NESTED)
    components, info = parse_svg_to_components(str(svg), target_width=80)
    assert len(components) == 2
    assert sum(len(c.holes) for c in components) == 2


def test_smiley_drops_background_and_keeps_face_with_eyes(tmp_path):
    svg = _write_svg(tmp_path, "smiley.svg", SMILEY)
    components, info = parse_svg_to_components(str(svg), target_width=100)
    # Face (with inner-white-hole) + 2 eyes (depth-2 bodies in the hole).
    assert len(components) == 3
    assert info["background_dropped"] == 1
    # The face is the largest area component and is the one with a hole.
    face = max(components, key=lambda c: c.area)
    assert len(face.holes) == 1
    assert face.source_fill == "#010102"
    eyes = [c for c in components if c is not face]
    assert all(len(e.holes) == 0 for e in eyes)


def test_full_bleed_badge_is_not_dropped(tmp_path):
    # The background guard requires len(components_remaining) > 1, so a
    # single full-canvas rectangle (legitimate badge) survives.
    svg = _write_svg(tmp_path, "badge.svg", FULL_BLEED_BADGE)
    components, info = parse_svg_to_components(str(svg), target_width=100)
    assert len(components) == 1
    assert info["background_dropped"] == 0
    assert components[0].source_fill == "#ff0000"


# --------------------------------------------------------------------------- #
# Packing — the critical correctness guard: nothing lands in a hole
# --------------------------------------------------------------------------- #


def test_no_pyramid_center_lands_in_a_hole(tmp_path):
    from stomppad.packing import create_pyramid_footprint

    svg = _write_svg(tmp_path, "donut.svg", DONUT)
    components, info = parse_svg_to_components(str(svg), target_width=60)
    component = components[0]
    positions = pack_component(
        component,
        pyramid_size=2,
        pyramid_spacing=0.5,
        safety_margin=0.2,
        skeleton_resolution=0.5,
    )
    assert positions, "expected at least one pyramid on the donut"
    holes = [Polygon(h) for h in component.holes]
    for x, y, rot in positions:
        for hole in holes:
            assert not hole.contains(Point(x, y)), (
                f"pyramid center at ({x:.2f},{y:.2f}) landed inside hole"
            )
            # The center check is necessary but not sufficient: a pyramid
            # whose center sits next to a hole could still have its
            # diamond footprint clip into the hole. Assert that too.
            footprint = create_pyramid_footprint(x, y, 2, rot)
            assert not hole.intersects(footprint), (
                f"pyramid footprint at ({x:.2f},{y:.2f}) overlapped hole"
            )


def test_pack_context_caches_skeleton():
    # Build a synthetic component manually so the test doesn't depend on
    # SVG parsing.
    exterior = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    component = Component(id=42, exterior=exterior, bbox=(0, 0, 10, 10), area=100)
    ctx = PackContext()
    a = ctx.skeleton_for(component, resolution=0.5)
    b = ctx.skeleton_for(component, resolution=0.5)
    assert a is b, "second call should return the cached list, not recompute"
    ctx.invalidate(component.id)
    c = ctx.skeleton_for(component, resolution=0.5)
    assert c is not a, "invalidate should force a recompute"


# --------------------------------------------------------------------------- #
# Patterns — each strategy must produce non-trivial output on a known rect
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["skeleton", "hexagonal", "rectangular", "triangular"])
def test_pattern_fills_a_rectangle(name):
    # 50×50 mm rectangle, 4mm pyramids, 1mm spacing → pitch 5mm.
    # Conservative lower bound: at least 25 pyramids fit (a 5×5 grid).
    exterior = [(0, 0), (50, 0), (50, 50), (0, 50), (0, 0)]
    component = Component(id=0, exterior=exterior, bbox=(0, 0, 50, 50), area=2500)
    positions = pack_component(
        component,
        pyramid_size=4,
        pyramid_spacing=1,
        safety_margin=0.5,
        pattern=name,
        skeleton_resolution=0.5,
    )
    assert len(positions) >= 25, (
        f"pattern={name!r} only placed {len(positions)} pyramids in a 50×50 rect"
    )


def test_unknown_pattern_raises_with_helpful_listing():
    with pytest.raises(KeyError) as exc:
        patterns.get("spiral")
    assert "spiral" in str(exc.value)
    # Error message includes the registered names so the user knows the options.
    for known in ("skeleton", "hexagonal", "rectangular", "triangular"):
        assert known in str(exc.value)


# --------------------------------------------------------------------------- #
# ShapeProject JSON round-trip
# --------------------------------------------------------------------------- #


def test_shape_project_round_trip(tmp_path):
    svg = _write_svg(tmp_path, "disjoint.svg", DISJOINT)
    components, info = parse_svg_to_components(str(svg), target_width=80)
    project = ShapeProject.default_from_components(
        components,
        info,
        global_params={"pyramid_size": 3, "pyramid_spacing": 1.5},
    )
    project.bodies[1].pattern = "hexagonal"
    project.bodies[2].enabled = False
    project.bodies[2].pyramid_overrides = {"pyramid_size": 1.5}

    js = project.to_json()
    rt = ShapeProject.from_json(js)

    assert rt.schema_version == project.schema_version
    assert len(rt.components) == len(project.components)
    assert len(rt.bodies) == len(project.bodies)
    assert rt.bodies[1].pattern == "hexagonal"
    assert rt.bodies[2].enabled is False
    assert rt.bodies[2].pyramid_overrides == {"pyramid_size": 1.5}
    assert rt.global_params["pyramid_size"] == 3
    # Component coordinates survive the round-trip.
    for orig, restored in zip(project.components, rt.components):
        assert orig.area == pytest.approx(restored.area)
        assert len(orig.exterior) == len(restored.exterior)


def test_shape_project_rejects_future_schema_version():
    payload = ShapeProject(schema_version=99999).to_dict()
    with pytest.raises(ValueError) as exc:
        ShapeProject.from_dict(payload)
    assert "schema_version" in str(exc.value)
