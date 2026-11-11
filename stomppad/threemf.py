"""Pack one mesh per body into a 3MF that a slicer opens as a single multi-part object."""

import io
import re
import struct
import zipfile
from dataclasses import dataclass, replace
from xml.sax.saxutils import escape, quoteattr

import numpy as np

MODEL_PATH = '3D/3dmodel.model'
BAMBU_SETTINGS_PATH = 'Metadata/model_settings.config'

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="config" ContentType="text/xml"/>
</Types>
"""
RELATIONSHIPS = f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/{MODEL_PATH}" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""

BINARY_STL_HEADER_BYTES = 84
BINARY_STL_FACET = np.dtype([('normal', '<f4', 3), ('corners', '<f4', (3, 3)), ('attribute', '<u2')])
MATERIALS_ID = 1
FIRST_MESH_ID = 2
IDENTITY_MATRIX = '1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1'
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)  # fixed, so the same pad always gives the same file


@dataclass
class MeshBody:
    name: str
    color: str             # '#rrggbb'
    filament: int          # slot the part prints from, counted from 1
    vertices: np.ndarray   # (N, 3), millimetres
    triangles: np.ndarray  # (M, 3) indices into vertices, counter-clockwise seen from outside


def _stl_corners(data):
    """
    Every facet's three corners as an (M, 3, 3) array, from binary or ASCII
    STL bytes. 32-bit either way: that is all a binary STL holds, and OpenSCAD
    writes its ASCII ones from 32-bit numbers too.
    """
    if len(data) >= BINARY_STL_HEADER_BYTES:
        facet_count = struct.unpack_from('<I', data, 80)[0]
        if len(data) == BINARY_STL_HEADER_BYTES + facet_count * BINARY_STL_FACET.itemsize:
            facets = np.frombuffer(data, BINARY_STL_FACET, facet_count, BINARY_STL_HEADER_BYTES)
            return facets['corners']
    numbers = re.findall(rb'vertex\s+(\S+)\s+(\S+)\s+(\S+)', data)
    if len(numbers) % 3:
        raise ValueError('STL ends in the middle of a facet.')
    return np.array(numbers, dtype='S').astype(np.float32).reshape(-1, 3, 3)


def read_stl(data):
    """
    (vertices, triangles) from STL bytes. An STL repeats each corner in every
    facet that uses it. 3MF wants each one listed once, which is also what
    tells a slicer the facets are joined.
    """
    corners = _stl_corners(data)
    if not np.isfinite(corners).all():
        raise ValueError('STL has coordinates that are not numbers.')
    vertices, index = np.unique(corners.reshape(-1, 3), axis=0, return_inverse=True)
    triangles = np.asarray(index).reshape(-1, 3)
    flat = (
        (triangles[:, 0] == triangles[:, 1])
        | (triangles[:, 1] == triangles[:, 2])
        | (triangles[:, 0] == triangles[:, 2])
    )
    triangles = triangles[~flat]
    if not len(triangles):
        raise ValueError('STL has no facets.')
    return vertices, triangles


def count_stl_facets(data):
    return len(_stl_corners(data))


def _printable(text):
    """Without control characters, which XML cannot carry even when escaped."""
    return ''.join(character for character in text if character.isprintable())


def _mesh_xml(body, object_id, with_color):
    color = f' pid="{MATERIALS_ID}" pindex="{body.filament - 1}"' if with_color else ''
    # numpy writes the shortest text that reads back as the same 32-bit number.
    vertices = ''.join(
        f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in np.asarray(body.vertices).astype(str).tolist()
    )
    triangles = ''.join(
        f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in body.triangles.tolist()
    )
    return (
        f'  <object id="{object_id}" type="model" name={quoteattr(body.name)}{color}>\n'
        f'   <mesh><vertices>{vertices}</vertices><triangles>{triangles}</triangles></mesh>\n'
        '  </object>\n'
    )


def _materials_xml(bodies):
    by_filament = {body.filament: body for body in bodies}
    bases = ''.join(
        f'<base name={quoteattr(by_filament[slot].name)} displaycolor="{by_filament[slot].color.upper()}"/>'
        if slot in by_filament else f'<base name="unused {slot}" displaycolor="#808080"/>'
        for slot in range(1, max(by_filament) + 1)
    )
    return f'  <basematerials id="{MATERIALS_ID}">{bases}</basematerials>\n'


def _model_xml(bodies, title, standard_colors):
    mesh_ids = range(FIRST_MESH_ID, FIRST_MESH_ID + len(bodies))
    pad_id = FIRST_MESH_ID + len(bodies)
    components = ''.join(f'<component objectid="{mesh_id}"/>' for mesh_id in mesh_ids)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
        f' <metadata name="Title">{escape(title)}</metadata>\n'
        ' <metadata name="Application">Stomp Pad Generator</metadata>\n'
        ' <resources>\n'
        + (_materials_xml(bodies) if standard_colors else '')
        + ''.join(_mesh_xml(body, mesh_id, standard_colors) for body, mesh_id in zip(bodies, mesh_ids))
        + f'  <object id="{pad_id}" type="model" name={quoteattr(title)}>'
        f'<components>{components}</components></object>\n'
        ' </resources>\n'
        f' <build><item objectid="{pad_id}"/></build>\n'
        '</model>\n'
    )


def _bambu_settings_xml(bodies, title):
    """
    What Bambu Studio reads part names and filaments from. A part's id is the
    id of its mesh object in the model file, and "extruder" is the filament
    slot.
    """
    pad_id = FIRST_MESH_ID + len(bodies)
    parts = ''.join(
        f'  <part id="{mesh_id}" subtype="normal_part">\n'
        f'   <metadata key="name" value={quoteattr(body.name)}/>\n'
        f'   <metadata key="matrix" value="{IDENTITY_MATRIX}"/>\n'
        f'   <metadata key="extruder" value="{body.filament}"/>\n'
        '  </part>\n'
        for mesh_id, body in enumerate(bodies, start=FIRST_MESH_ID)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<config>\n'
        f' <object id="{pad_id}">\n'
        f'  <metadata key="name" value={quoteattr(title)}/>\n'
        f'  <metadata key="extruder" value="{bodies[0].filament}"/>\n'
        f'{parts}'
        ' </object>\n'
        '</config>\n'
    )


def write_3mf(bodies, title='Stomp pad', standard_colors=False):
    """
    3MF bytes for one object made of a part per body.

    Any slicer opens it as plain parts named after their colors. Bambu Studio
    also reads each part's filament slot from Metadata/model_settings.config,
    so the colors land on the right filaments without assigning them by hand.

    standard_colors adds each part's color the way the 3MF core spec defines
    it, for viewers and other slicers. Recent Bambu Studio versions then open
    their color-matching dialog and ignore the filament slots.
    """
    bodies = [replace(body, name=_printable(body.name)) for body in bodies]
    title = _printable(title)
    if not bodies:
        raise ValueError('A 3MF needs at least one body.')
    files = {
        '[Content_Types].xml': CONTENT_TYPES,
        '_rels/.rels': RELATIONSHIPS,
        MODEL_PATH: _model_xml(bodies, title, standard_colors),
        BAMBU_SETTINGS_PATH: _bambu_settings_xml(bodies, title),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, text in files.items():
            entry = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, text.encode('utf-8'))
    return buffer.getvalue()


def pad_3mf(design, stl_by_body, title='Stomp pad', standard_colors=False):
    """3MF bytes for a pad design, given each body's rendered STL bytes as {body number: bytes}."""
    missing = [body.number for body in design.bodies if body.number not in stl_by_body]
    if missing:
        raise ValueError(f'No STL for body {missing}.')
    return write_3mf(
        [
            MeshBody(body.label, body.fill, body.number, *read_stl(stl_by_body[body.number]))
            for body in design.bodies
        ],
        title, standard_colors,
    )
