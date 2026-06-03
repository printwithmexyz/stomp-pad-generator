"""One-shot script used to capture the regression-test golden output.

Run from the repo root:

    .venv/Scripts/python tests/fixtures/_capture_golden.py

Re-run whenever the *intentional* output of the pipeline changes (e.g. after
Phase 1 lands the component-aware parser and the captured output should
shift). The pytest regression test (tests/test_regression.py) replays the
same pipeline and asserts byte-identical output against the captured file.

This script is intentionally separate from the test so the test stays
read-only and the capture step is explicit / auditable.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from stomppad import (  # noqa: E402
    calculate_skeleton,
    calculate_valid_pyramid_positions,
    generate_openscad_with_positions,
    parse_svg_to_polygon,
)

HERE = Path(__file__).parent
SVG = HERE / "sample.svg"
OUT = HERE / "sample_golden.scad"

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


def main() -> None:
    polygon, svg_info = parse_svg_to_polygon(
        str(SVG),
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
    generate_openscad_with_positions(
        SVG.name,
        positions,
        str(OUT),
        svg_info=svg_info,
        base_thickness=PARAMS["base_thickness"],
        outline_offset=PARAMS["outline_offset"],
        outline_height=PARAMS["outline_height"],
        pyramid_size=PARAMS["pyramid_size"],
        pyramid_height=PARAMS["pyramid_height"],
        pyramid_style=PARAMS["pyramid_style"],
    )
    print(f"captured: {OUT}  ({OUT.stat().st_size} bytes, {len(positions)} pyramids)")


if __name__ == "__main__":
    main()
