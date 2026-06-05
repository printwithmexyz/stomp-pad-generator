"""Generic 3MF exporter.

Phase 3.3: pack per-body STL bytes into a single ``.3mf`` archive that
imports into any modern slicer (PrusaSlicer / Bambu Studio / Orca / Cura)
with the per-body color preserved on each ``<object>``.

The format is hand-rolled (no new dep, per the v2-plan constraint):

- A 3MF is just a zip with three pieces of XML — ``[Content_Types].xml``,
  ``_rels/.rels``, ``3D/3dmodel.model``. The first two are boilerplate
  for any 3MF; only the third actually carries the geometry.
- Each STL is parsed (binary format — OpenSCAD's default) into a flat
  triangle list, then deduplicated into the indexed
  ``<vertices>`` / ``<triangles>`` shape 3MF wants.
- Color rides on a ``basematerials/base`` resource per body with
  ``displaycolor`` set to the body's hex (alpha defaulted to ``FF``).
  This is the simplest cross-slicer path. The v2 plan warns that slicer
  support for color metadata varies — the schema check here only
  confirms structural validity, not visual fidelity in a specific
  slicer. Phase 3 acceptance still needs a manual import check.
"""
from __future__ import annotations

import io
import struct
import zipfile
from typing import Optional
from xml.sax.saxutils import escape

from ..project import Body, ShapeProject


THREEMF_CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
THREEMF_REL_TYPE = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"

CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>
"""

RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel" Target="/3D/3dmodel.model"/>
</Relationships>
"""


def _to_3mf_color(hex_color: Optional[str]) -> str:
    """Normalize a CSS-style ``#rrggbb`` (or ``#rrggbbaa``) into the 3MF
    8-hex ``displaycolor`` form. Defaults alpha to ``FF`` and falls back
    to a neutral grey for blank input."""
    if not hex_color:
        return "#CCCCCCFF"
    c = hex_color.strip().lstrip("#").upper()
    if len(c) == 6:
        return f"#{c}FF"
    if len(c) == 8:
        return f"#{c}"
    return "#CCCCCCFF"


def _parse_stl(data: bytes) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Parse an STL (ASCII or binary) into deduplicated vertices +
    triangle indices.

    Tries ASCII first if the data starts with ``solid`` (the ASCII
    sentinel); a real binary STL can also start with those bytes inside
    its 80-byte junk header, so a failed ASCII parse falls through to
    the binary path. openscad-wasm 2025's manifold backend has been
    observed to emit ASCII STL even though OpenSCAD's CLI default is
    binary, so this fallback is load-bearing for the web Export flow.
    """
    if data[:5] == b"solid":
        try:
            verts, tris = _parse_stl_ascii(data)
            if tris:
                return verts, tris
        except ValueError:
            pass  # try binary
    return _parse_stl_binary(data)


def _parse_stl_ascii(data: bytes) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Parse an ASCII STL. Permissive: ignores whitespace + ``solid`` /
    ``facet`` / ``outer loop`` / ``endloop`` / ``endfacet`` / ``endsolid``
    keywords. Only ``vertex x y z`` lines contribute geometry."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("not ASCII STL (UTF-8 decode failed)")
    vertices: list[tuple[float, float, float]] = []
    triangles: list[tuple[int, int, int]] = []
    vertex_to_index: dict[tuple[float, float, float], int] = {}
    current_tri: list[int] = []
    for line in text.split("\n"):
        s = line.strip()
        if not s.startswith("vertex"):
            continue
        parts = s.split()
        if len(parts) < 4:
            continue
        try:
            v = (float(parts[1]), float(parts[2]), float(parts[3]))
        except ValueError:
            continue
        idx = vertex_to_index.get(v)
        if idx is None:
            idx = len(vertices)
            vertex_to_index[v] = idx
            vertices.append(v)
        current_tri.append(idx)
        if len(current_tri) == 3:
            triangles.append((current_tri[0], current_tri[1], current_tri[2]))
            current_tri = []
    return vertices, triangles


def _parse_stl_binary(data: bytes) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Parse a binary STL into ``(vertices, triangles)`` with shared
    vertices deduplicated by exact coordinate match. Triangle indices
    reference into the vertex list.

    Detection is **structural**, not header-sniffing: a binary STL's
    payload size is exactly ``84 + triangle_count * 50``. Use
    :func:`_parse_stl` rather than calling this directly — it dispatches
    between ASCII and binary based on the file's actual format.
    """
    if len(data) < 84:
        raise ValueError(f"STL too short ({len(data)} bytes); not a binary STL")
    triangle_count = struct.unpack_from("<I", data, 80)[0]
    expected = 84 + triangle_count * 50
    if len(data) != expected:
        # Doesn't match the binary layout. If it also starts with "solid",
        # it's almost certainly an ASCII STL — give a targeted error.
        if data[:5] == b"solid":
            raise ValueError("ASCII STL is not supported — re-export as binary")
        raise ValueError(
            f"STL malformed: claims {triangle_count} triangles "
            f"(expected {expected} bytes, got {len(data)})"
        )

    vertices: list[tuple[float, float, float]] = []
    triangles: list[tuple[int, int, int]] = []
    vertex_to_index: dict[tuple[float, float, float], int] = {}
    offset = 84
    for _ in range(triangle_count):
        offset += 12  # skip normal
        tri = []
        for _ in range(3):
            v = struct.unpack_from("<3f", data, offset)
            offset += 12
            idx = vertex_to_index.get(v)
            if idx is None:
                idx = len(vertices)
                vertex_to_index[v] = idx
                vertices.append(v)
            tri.append(idx)
        triangles.append((tri[0], tri[1], tri[2]))
        offset += 2  # skip attribute byte count
    return vertices, triangles


def _mesh_xml(vertices, triangles) -> str:
    """Render a ``<mesh>`` block with the indexed-mesh format 3MF expects."""
    vlines = "\n      ".join(
        f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>'
        for x, y, z in vertices
    )
    tlines = "\n      ".join(
        f'<triangle v1="{a}" v2="{b}" v3="{c}"/>'
        for a, b, c in triangles
    )
    return (
        "    <mesh>\n"
        "      <vertices>\n"
        f"      {vlines}\n"
        "      </vertices>\n"
        "      <triangles>\n"
        f"      {tlines}\n"
        "      </triangles>\n"
        "    </mesh>"
    )


def _build_3dmodel_xml(project: ShapeProject, body_meshes: list[tuple[Body, list, list]]) -> str:
    """Build the ``3D/3dmodel.model`` payload from per-body parsed meshes."""
    # Per-body base materials. Each <base> entry gets one slot; objects
    # reference materials by pid (resource id) + pindex (slot within).
    base_lines = "\n      ".join(
        f'<base name="{escape(body.name)}" displaycolor="{_to_3mf_color(body.color_hex)}"/>'
        for body, _vertices, _triangles in body_meshes
    )
    basematerials = (
        f'    <basematerials id="1">\n      {base_lines}\n    </basematerials>'
    )

    object_blocks = []
    item_blocks = []
    for index, (body, vertices, triangles) in enumerate(body_meshes):
        object_id = 100 + index  # any unique positive int; 100+ to avoid clashing with resource ids
        object_blocks.append(
            f'    <object id="{object_id}" name="{escape(body.name)}" '
            f'type="model" pid="1" pindex="{index}">\n'
            f"{_mesh_xml(vertices, triangles)}\n"
            f"    </object>"
        )
        item_blocks.append(f'    <item objectid="{object_id}"/>')

    resources = "\n".join([basematerials] + object_blocks)
    items = "\n".join(item_blocks)

    # NB: 3MF Core spec §4.1.2 wants <metadata> AFTER <resources> + <build>
    # (or nested inside them). PrusaSlicer / Bambu Studio are lenient; Cura's
    # validator is stricter. Place at the end so all three accept.
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US"
       xmlns="{THREEMF_CORE_NS}">
  <resources>
{resources}
  </resources>
  <build>
{items}
  </build>
  <metadata name="Application">stomppad</metadata>
</model>
"""


def build_threemf(
    project: ShapeProject,
    stl_per_body: dict,
    *,
    logger=None,
) -> bytes:
    """Build a 3MF (zip) byte blob from per-body STLs.

    Skips disabled bodies and any body without an STL entry. Raises
    :class:`ValueError` if no bodies survive that filter (an empty 3MF
    is malformed). Logs a warning (via ``logger`` if provided, else
    silently skips) when a body's STL parses to zero triangles — the
    file format is valid but contains no mesh, which would otherwise be
    a silent divergence from ``build_stl_set``.

    Accepts ``stl_per_body`` keyed by either ``int`` or ``str`` — JS objects
    coerced through ``pyodide.toPy`` arrive with string keys, and a strict
    int lookup would silently drop every entry.
    """
    stl_per_body = {int(k): v for k, v in stl_per_body.items()}
    body_meshes: list[tuple[Body, list, list]] = []
    for body in project.bodies:
        if not body.enabled or body.id not in stl_per_body:
            continue
        verts, tris = _parse_stl(stl_per_body[body.id])
        if not tris:
            if logger is not None:
                logger(
                    f"build_threemf: body {body.id} ({body.name}) has zero "
                    "triangles; skipping (the per-body STL was empty)."
                )
            continue
        body_meshes.append((body, verts, tris))
    if not body_meshes:
        raise ValueError(
            "build_threemf: no enabled bodies with non-empty STLs to export."
        )

    model_xml = _build_3dmodel_xml(project, body_meshes)

    buf = io.BytesIO()
    # DEFLATE keeps the .3mf small (model.xml compresses well — coordinate
    # strings repeat a lot). The web-side STL-set zip uses STORE because the
    # browser would otherwise need a deflate impl too; for the Python-side
    # 3MF the stdlib gives us deflate for free.
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", RELS_XML)
        zf.writestr("3D/3dmodel.model", model_xml)
    return buf.getvalue()
