"""Phase 3 acceptance tests.

Covers v2-plan Phase 3.1–3.3:
- ``generate_body_scad`` shape + ``generate_per_body_scads`` filtering
- STL-set bundle contents + filename slug-ification + print guide
- 3MF zip validity (three required pieces) + per-object color preservation
- STL binary parsing + vertex deduplication

The slicer-acceptance check called out in the v2 plan ("manual import in
PrusaSlicer shows N objects with the right colors") is intentionally
out of scope here — the schema/structure check below confirms the file
is well-formed, but a real slicer round-trip stays a manual gate.
"""
from __future__ import annotations

import io
import struct
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from stomppad import (  # noqa: E402
    BodyOutputCache,
    ShapeProject,
    generate_body_scad,
    generate_per_body_scads,
    parse_svg_to_components,
)
from stomppad.exporters import build_stl_set, build_threemf, write_stl_set  # noqa: E402
from stomppad.exporters.stl_set import _safe_filename  # noqa: E402
from stomppad.exporters.threemf import (  # noqa: E402
    _parse_stl_binary,
    _to_3mf_color,
)


DISJOINT = """<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect x="10" y="10" width="20" height="20" fill="#ff0000"/>
  <rect x="40" y="10" width="20" height="20" fill="#00ff00"/>
  <rect x="70" y="10" width="20" height="20" fill="#0000ff"/>
</svg>"""

DONUT = """<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect x="20" y="20" width="60" height="60" fill="#aa00aa"/>
  <rect x="40" y="40" width="20" height="20" fill="#ffffff"/>
</svg>"""


def _project(tmp_path: Path, name: str, body: str) -> ShapeProject:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    components, info = parse_svg_to_components(str(p), target_width=80)
    return ShapeProject.default_from_components(components, info)


def _synthetic_stl(triangles: list[tuple[tuple[float, float, float], ...]]) -> bytes:
    """Build a minimal binary STL from a list of triangles (each = 3 vertex
    tuples). Used in tests so we don't need OpenSCAD installed.
    """
    buf = io.BytesIO()
    buf.write(b"\x00" * 80)  # header
    buf.write(struct.pack("<I", len(triangles)))
    for v1, v2, v3 in triangles:
        buf.write(struct.pack("<3f", 0.0, 0.0, 1.0))  # normal (junk)
        for v in (v1, v2, v3):
            buf.write(struct.pack("<3f", *v))
        buf.write(b"\x00\x00")  # attribute byte count
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# 3.1 — multi-body SCAD generation
# --------------------------------------------------------------------------- #


def test_generate_body_scad_contains_inline_polygon(tmp_path):
    project = _project(tmp_path, "disjoint.svg", DISJOINT)
    cache = BodyOutputCache(project)
    body = project.bodies[0]
    scad = generate_body_scad(
        project, body, cache.positions_for(body.id),
    )
    assert "polygon(points=[" in scad
    assert "body_part();" in scad
    # The component's exterior coords should appear (smoke check that
    # _component_to_polygon_scad ran).
    comp = project.component(body.component_ids[0])
    sample_x = comp.exterior[0][0]
    assert f"{sample_x:.3f}" in scad


def test_generate_body_scad_with_holes_emits_difference(tmp_path):
    project = _project(tmp_path, "donut.svg", DONUT)
    cache = BodyOutputCache(project)
    body = project.bodies[0]
    scad = generate_body_scad(
        project, body, cache.positions_for(body.id),
    )
    assert "difference()" in scad, "holes should be rendered via difference()"


def test_generate_per_body_scads_skips_disabled(tmp_path):
    project = _project(tmp_path, "disjoint.svg", DISJOINT)
    cache = BodyOutputCache(project)
    project.set_body_enabled(1, False)
    out = generate_per_body_scads(project, cache)
    assert set(out.keys()) == {0, 2}
    assert all("body_part();" in scad for scad in out.values())


def test_generate_body_scad_empty_body_still_valid(tmp_path):
    project = _project(tmp_path, "disjoint.svg", DISJOINT)
    # Build an empty body — Phase 2 group_selected_into_new_body normally
    # prevents this, but the SCAD generator must still produce something
    # syntactically valid for defensive callers.
    from stomppad import Body
    empty = Body(id=99, name="empty", component_ids=[], color_hex=None)
    scad = generate_body_scad(project, empty, [])
    assert "body_part();" in scad
    assert "(no components)" in scad


# --------------------------------------------------------------------------- #
# 3.2 — STL-set exporter
# --------------------------------------------------------------------------- #


def test_safe_filename_slugifies():
    assert _safe_filename("Big Red") == "big-red"
    assert _safe_filename("body/0?!") == "body-0"
    assert _safe_filename("") == "body"
    assert _safe_filename("---") == "body"


def test_build_stl_set_contains_one_stl_per_enabled_body(tmp_path):
    project = _project(tmp_path, "disjoint.svg", DISJOINT)
    stl = _synthetic_stl([
        ((0, 0, 0), (1, 0, 0), (0, 1, 0)),
    ])
    project.set_body_enabled(1, False)
    bundle = build_stl_set(
        project,
        {0: stl, 1: stl, 2: stl},  # body 1 IS in the dict but disabled — drop it
    )
    stl_names = [n for n in bundle if n.endswith(".stl")]
    assert len(stl_names) == 2
    assert "print-guide.txt" in bundle
    guide = bundle["print-guide.txt"].decode("utf-8")
    assert "body_0" in guide
    assert "Skipped (disabled)" in guide
    assert "body_1" in guide  # listed as skipped


def test_build_stl_set_disambiguates_duplicate_names(tmp_path):
    project = _project(tmp_path, "disjoint.svg", DISJOINT)
    # Force two bodies to share a name.
    project.bodies[0].name = "duplicate"
    project.bodies[1].name = "duplicate"
    stl = _synthetic_stl([((0, 0, 0), (1, 0, 0), (0, 1, 0))])
    bundle = build_stl_set(project, {0: stl, 1: stl, 2: stl})
    stl_names = [n for n in bundle if n.endswith(".stl")]
    assert len(stl_names) == 3
    assert len(set(stl_names)) == 3, "duplicate names must collide-free"


def test_write_stl_set_writes_files(tmp_path):
    project = _project(tmp_path, "disjoint.svg", DISJOINT)
    stl = _synthetic_stl([((0, 0, 0), (1, 0, 0), (0, 1, 0))])
    out_dir = tmp_path / "out"
    paths = write_stl_set(project, {0: stl, 1: stl, 2: stl}, out_dir)
    for name, path in paths.items():
        assert path.exists()
        assert path.parent == out_dir


# --------------------------------------------------------------------------- #
# 3.3 — 3MF exporter
# --------------------------------------------------------------------------- #


def test_to_3mf_color_normalizes_alpha():
    assert _to_3mf_color("#abc123") == "#ABC123FF"
    assert _to_3mf_color("#abc12345") == "#ABC12345"
    assert _to_3mf_color(None) == "#CCCCCCFF"
    assert _to_3mf_color("garbage") == "#CCCCCCFF"


def test_parse_stl_binary_dedupes_vertices():
    # A square = 2 triangles sharing two vertices; 6 raw points → 4 unique.
    tris = [
        ((0, 0, 0), (1, 0, 0), (1, 1, 0)),
        ((0, 0, 0), (1, 1, 0), (0, 1, 0)),
    ]
    stl = _synthetic_stl(tris)
    verts, triangles = _parse_stl_binary(stl)
    assert len(verts) == 4
    assert len(triangles) == 2
    # Every index must reference an existing vertex.
    for tri in triangles:
        for idx in tri:
            assert 0 <= idx < len(verts)


def test_parse_stl_binary_rejects_truncated():
    truncated = b"\x00" * 80 + struct.pack("<I", 10)  # claims 10 tris, data ends here
    with pytest.raises(ValueError, match="truncated"):
        _parse_stl_binary(truncated)


def test_parse_stl_binary_rejects_ascii():
    ascii_stl = (
        b"solid demo\nfacet normal 0 0 1\nouter loop\n"
        b"vertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\n"
        b"endloop\nendfacet\nendsolid demo\n"
    )
    # Pad to >84 bytes so the length guard isn't what trips.
    padded = ascii_stl + b"\x00" * 200
    with pytest.raises(ValueError, match="ASCII"):
        _parse_stl_binary(padded)


def test_build_threemf_zip_structure(tmp_path):
    project = _project(tmp_path, "disjoint.svg", DISJOINT)
    stl = _synthetic_stl([((0, 0, 0), (1, 0, 0), (0, 1, 0))])
    blob = build_threemf(project, {0: stl, 1: stl, 2: stl})
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = set(zf.namelist())
        assert names == {"[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model"}
        model = zf.read("3D/3dmodel.model").decode("utf-8")
    # Strip namespace for easier XML assertions.
    root = ET.fromstring(model)
    ns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
    objects = root.findall(".//m:object", ns)
    assert len(objects) == 3
    bases = root.findall(".//m:base", ns)
    assert len(bases) == 3
    colors = [b.get("displaycolor") for b in bases]
    # All three rects had distinct fills; all should survive.
    assert all(c and c.startswith("#") and len(c) == 9 for c in colors)
    items = root.findall(".//m:item", ns)
    assert len(items) == 3


def test_build_threemf_skips_disabled_bodies(tmp_path):
    project = _project(tmp_path, "disjoint.svg", DISJOINT)
    project.set_body_enabled(1, False)
    stl = _synthetic_stl([((0, 0, 0), (1, 0, 0), (0, 1, 0))])
    blob = build_threemf(project, {0: stl, 1: stl, 2: stl})
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        model = zf.read("3D/3dmodel.model").decode("utf-8")
    root = ET.fromstring(model)
    objects = root.findall(
        ".//{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}object"
    )
    assert len(objects) == 2


def test_build_threemf_no_bodies_raises(tmp_path):
    project = _project(tmp_path, "disjoint.svg", DISJOINT)
    for body in project.bodies:
        project.set_body_enabled(body.id, False)
    with pytest.raises(ValueError, match="no enabled bodies"):
        build_threemf(project, {})


# --------------------------------------------------------------------------- #
# Web path: stl_per_body arrives with string keys via pyodide.toPy(jsObject).
# The reviewer caught this — without normalization the exporters silently
# drop every entry. Lock the contract with explicit string-keyed tests.
# --------------------------------------------------------------------------- #


def test_build_stl_set_accepts_string_keys(tmp_path):
    project = _project(tmp_path, "disjoint.svg", DISJOINT)
    stl = _synthetic_stl([((0, 0, 0), (1, 0, 0), (0, 1, 0))])
    bundle = build_stl_set(project, {"0": stl, "1": stl, "2": stl})
    stl_names = [n for n in bundle if n.endswith(".stl")]
    assert len(stl_names) == 3, "string-keyed dict (from JS) must work"


def test_build_threemf_accepts_string_keys(tmp_path):
    project = _project(tmp_path, "disjoint.svg", DISJOINT)
    stl = _synthetic_stl([((0, 0, 0), (1, 0, 0), (0, 1, 0))])
    blob = build_threemf(project, {"0": stl, "1": stl, "2": stl})
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        model = zf.read("3D/3dmodel.model").decode("utf-8")
    root = ET.fromstring(model)
    objects = root.findall(
        ".//{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}object"
    )
    assert len(objects) == 3, "string-keyed dict (from JS) must work"
