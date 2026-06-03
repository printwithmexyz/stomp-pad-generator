"""Phase 0 regression guard.

Asserts that the geometry pipeline still produces byte-identical OpenSCAD
output for a captured fixture after moving the code into the ``stomppad``
package. Phase 1.5 will replace this single-fixture test with the broader
suite called out in the v2 plan.

Byte-equality is the assertion the v2 plan called for. To keep it stable
across environments, ``requirements-dev.txt`` pins the numerical
dependencies whose minor-version drift can flip the f"{x:.3f}" formatting
of a pyramid position (notably numpy + scikit-image). If this test fails
after an intentional output change, recapture the golden via
``python tests/fixtures/_capture_golden.py`` and review the diff before
committing the new golden.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from stomppad import (  # noqa: E402
    calculate_skeleton,
    calculate_valid_pyramid_positions,
    generate_openscad_with_positions,
    parse_svg_to_polygon,
)

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_SVG = FIXTURES / "sample.svg"
GOLDEN_SCAD = FIXTURES / "sample_golden.scad"

# Mirror tests/fixtures/_capture_golden.py exactly. Drift here would silently
# defeat the regression check, so keep both in lockstep.
PARAMS = dict(
    target_width=40,
    samples_per_segment=20,
    skeleton_resolution=0.5,
    pyramid_size=4,
    pyramid_spacing=1,
    safety_margin=0.5,
    include_rotation=True,
    base_thickness=2,
    outline_offset=2,
    outline_height=0.8,
    pyramid_height=2.5,
    pyramid_style=4,
)


def test_sample_svg_produces_golden_scad(tmp_path):
    polygon, svg_info = parse_svg_to_polygon(
        str(SAMPLE_SVG),
        target_width=PARAMS["target_width"],
        samples_per_segment=PARAMS["samples_per_segment"],
    )
    skeleton = calculate_skeleton(polygon, resolution=PARAMS["skeleton_resolution"])
    positions = calculate_valid_pyramid_positions(
        polygon,
        pyramid_size=PARAMS["pyramid_size"],
        pyramid_spacing=PARAMS["pyramid_spacing"],
        target_width=PARAMS["target_width"],
        include_rotation=PARAMS["include_rotation"],
        safety_margin=PARAMS["safety_margin"],
        skeleton_points=skeleton,
    )
    out = tmp_path / "actual.scad"
    generate_openscad_with_positions(
        SAMPLE_SVG.name,
        positions,
        str(out),
        svg_info=svg_info,
        base_thickness=PARAMS["base_thickness"],
        outline_offset=PARAMS["outline_offset"],
        outline_height=PARAMS["outline_height"],
        pyramid_size=PARAMS["pyramid_size"],
        pyramid_height=PARAMS["pyramid_height"],
        pyramid_style=PARAMS["pyramid_style"],
    )

    actual = out.read_bytes()
    golden = GOLDEN_SCAD.read_bytes()
    assert actual == golden, (
        f"scad output drifted: actual {len(actual)} bytes vs "
        f"golden {len(golden)} bytes. "
        "Run `python tests/fixtures/_capture_golden.py` to recapture if "
        "the output change is intentional."
    )


def test_shim_reexports_match_package():
    """The back-compat shim should expose the same callables as the package."""
    import pyramid_position_calculator as shim
    import stomppad

    public = [
        "parse_svg_to_polygon",
        "calculate_skeleton",
        "calculate_centerline_tangent",
        "create_pyramid_footprint",
        "calculate_valid_pyramid_positions",
        "generate_openscad_with_positions",
        "save_debug_visualization",
        "main",
    ]
    for name in public:
        assert getattr(shim, name) is getattr(stomppad, name), (
            f"shim.{name} drifted from stomppad.{name}"
        )
