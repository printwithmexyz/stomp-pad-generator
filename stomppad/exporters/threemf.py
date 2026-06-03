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


def _parse_stl_binary(data: bytes) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Parse a binary STL into ``(vertices, triangles)`` with shared
    vertices deduplicated by exact coordinate match. Triangle indices
    reference into the vertex list.

    ASCII STL is rejected — OpenSCAD emits binary by default and the
    callers in this project all use the default. Add an ASCII path only
    when a real user file demands it.
    """
    if len(data) < 84:
        raise ValueError(f"STL too short ({len(data)} bytes); not a binary STL")
    if data[:5] == b"solid" and b"facet" in data[:512]:
        # Heuristic: a binary STL's 80-byte header *can* start with "solid"
        # too, but a binary file never contains "facet" in the header. If
        # both conditions hit, this is an ASCII STL we can't parse.
        raise ValueError("ASCII STL is not supported — re-export as binary")
    triangle_count = struct.unpack_from("<I", data, 80)[0]
    expected = 84 + triangle_count * 50
    if len(data) < expected:
        raise ValueError(
            f"STL truncated: claims {triangle_count} triangles "
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
) -> bytes:
    """Build a 3MF (zip) byte blob from per-body STLs.

    Skips disabled bodies and any body without an STL entry. Raises
    :class:`ValueError` if no bodies survive that filter (an empty 3MF
    is malformed).

    Accepts ``stl_per_body`` keyed by either ``int`` or ``str`` — JS objects
    coerced through ``pyodide.toPy`` arrive with string keys, and a strict
    int lookup would silently drop every entry.
    """
    stl_per_body = {int(k): v for k, v in stl_per_body.items()}
    body_meshes: list[tuple[Body, list, list]] = []
    for body in project.bodies:
        if not body.enabled or body.id not in stl_per_body:
            continue
        verts, tris = _parse_stl_binary(stl_per_body[body.id])
        if not tris:
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
