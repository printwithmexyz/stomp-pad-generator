import io
import struct
import xml.etree.ElementTree as ET
import zipfile

import numpy as np
import pytest

from stomppad.pad import PadSettings, build_pad
from stomppad.threemf import MeshBody, pad_3mf, read_stl, write_3mf

CORE = '{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}'
TETRAHEDRON = np.array([
    [[0, 0, 0], [0, 1, 0], [1, 0, 0]],
    [[0, 0, 0], [1, 0, 0], [0, 0, 1]],
    [[0, 0, 0], [0, 0, 1], [0, 1, 0]],
    [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
], dtype=float)


def ascii_stl(facets, shift=0.0):
    lines = ['solid test']
    for facet in facets + shift:
        lines += ['facet normal 0 0 0', 'outer loop']
        lines += [f'vertex {x:.6g} {y:.6g} {z:.6g}' for x, y, z in facet]
        lines += ['endloop', 'endfacet']
    return '\n'.join([*lines, 'endsolid test']).encode()


def binary_stl(facets):
    data = b'\0' * 80 + struct.pack('<I', len(facets))
    for facet in facets:
        data += struct.pack('<12fH', 0, 0, 0, *facet.ravel(), 0)
    return data


def mesh_body(name, color, filament, shift=0.0):
    return MeshBody(name, color, filament, *read_stl(ascii_stl(TETRAHEDRON, shift)))


def unpack(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name).decode() for name in archive.namelist()}


def model_objects(files):
    resources = ET.fromstring(files['3D/3dmodel.model']).find(f'{CORE}resources')
    return resources.findall(f'{CORE}object')


@pytest.mark.parametrize('encode', [ascii_stl, binary_stl])
def test_stl_corners_are_merged_into_shared_vertices(encode):
    vertices, triangles = read_stl(encode(TETRAHEDRON))
    assert len(vertices) == 4
    assert triangles.shape == (4, 3)
    assert vertices[triangles] == pytest.approx(TETRAHEDRON)


def test_stl_facets_with_no_area_are_dropped():
    flat = np.array([[[0, 0, 0], [1, 1, 1], [1, 1, 1]]], dtype=float)
    _, triangles = read_stl(ascii_stl(np.concatenate([TETRAHEDRON, flat])))
    assert len(triangles) == 4


@pytest.mark.parametrize('data', [b'', b'solid empty\nendsolid empty\n', b'not an stl at all'])
def test_stl_without_facets_is_an_error(data):
    with pytest.raises(ValueError):
        read_stl(data)


def test_stl_with_coordinates_that_are_not_numbers_is_an_error():
    broken = ascii_stl(TETRAHEDRON).replace(b'vertex 1 0 0', b'vertex nan 0 0')
    with pytest.raises(ValueError, match='not numbers'):
        read_stl(broken)


def test_3mf_holds_one_mesh_per_body_joined_into_one_object():
    files = unpack(write_3mf([mesh_body('1 base #111111', '#111111', 1), mesh_body('2 #ff0000', '#ff0000', 2, 5)]))
    assert list(files) == ['[Content_Types].xml', '_rels/.rels', '3D/3dmodel.model', 'Metadata/model_settings.config']
    assert 'Target="/3D/3dmodel.model"' in files['_rels/.rels']

    model = ET.fromstring(files['3D/3dmodel.model'])
    assert model.get('unit') == 'millimeter'
    *meshes, pad = model_objects(files)
    assert [mesh.get('name') for mesh in meshes] == ['1 base #111111', '2 #ff0000']
    for mesh in meshes:
        vertices = mesh.findall(f'{CORE}mesh/{CORE}vertices/{CORE}vertex')
        triangles = mesh.findall(f'{CORE}mesh/{CORE}triangles/{CORE}triangle')
        assert (len(vertices), len(triangles)) == (4, 4)
        assert all(0 <= int(triangle.get(corner)) < 4 for triangle in triangles for corner in ('v1', 'v2', 'v3'))
    assert [float(v.get('x')) for v in meshes[1].iter(f'{CORE}vertex')] == [5, 5, 5, 6]

    components = [c.get('objectid') for c in pad.iter(f'{CORE}component')]
    assert components == [mesh.get('id') for mesh in meshes]
    assert [item.get('objectid') for item in model.iter(f'{CORE}item')] == [pad.get('id')]


def test_bambu_settings_give_each_part_its_filament():
    bodies = [mesh_body('1 base #111111', '#111111', 1), mesh_body('2 #ff0000', '#ff0000', 2),
              mesh_body('3 #00ff00', '#00ff00', 3)]
    files = unpack(write_3mf(bodies, title='logo'))
    *meshes, pad = model_objects(files)

    settings = ET.fromstring(files['Metadata/model_settings.config'])
    (pad_settings,) = settings.findall('object')
    assert pad_settings.get('id') == pad.get('id')
    assert pad_settings.find("metadata[@key='name']").get('value') == 'logo'
    parts = pad_settings.findall('part')
    assert [part.get('id') for part in parts] == [mesh.get('id') for mesh in meshes]
    assert [part.get('subtype') for part in parts] == ['normal_part'] * 3
    assert [part.find("metadata[@key='extruder']").get('value') for part in parts] == ['1', '2', '3']
    assert [part.find("metadata[@key='name']").get('value') for part in parts] == [body.name for body in bodies]


def test_standard_colors_are_left_out_unless_asked_for():
    bodies = [mesh_body('1 base #111111', '#111111', 1), mesh_body('2 #ff0000', '#ff0000', 2)]
    plain = unpack(write_3mf(bodies))['3D/3dmodel.model']
    assert 'basematerials' not in plain and 'pid=' not in plain

    colored = unpack(write_3mf(bodies, standard_colors=True))
    resources = ET.fromstring(colored['3D/3dmodel.model']).find(f'{CORE}resources')
    materials = resources.find(f'{CORE}basematerials')
    assert [base.get('displaycolor') for base in materials] == ['#111111', '#FF0000']
    *meshes, pad = resources.findall(f'{CORE}object')
    assert [(mesh.get('pid'), mesh.get('pindex')) for mesh in meshes] == [(materials.get('id'), '0'), (materials.get('id'), '1')]
    assert pad.get('pid') is None
    ids = [materials.get('id')] + [mesh.get('id') for mesh in meshes] + [pad.get('id')]
    assert len(set(ids)) == len(ids)


def test_names_with_markup_characters_survive():
    files = unpack(write_3mf([mesh_body('a "quoted" <name> & more', '#000000', 1)], title='R&D <pad>'))
    model = ET.fromstring(files['3D/3dmodel.model'])
    assert model_objects(files)[0].get('name') == 'a "quoted" <name> & more'
    assert model.find(f"{CORE}metadata[@name='Title']").text == 'R&D <pad>'
    settings = ET.fromstring(files['Metadata/model_settings.config'])
    assert settings.find("object/part/metadata[@key='name']").get('value') == 'a "quoted" <name> & more'


def test_control_characters_in_names_are_dropped():
    files = unpack(write_3mf([mesh_body('bell\x07 name', '#000000', 1)], title='tab\x0bbed'))
    assert model_objects(files)[0].get('name') == 'bell name'
    assert ET.fromstring(files['Metadata/model_settings.config']).find("object/metadata[@key='name']").get('value') == 'tabbed'


def test_same_bodies_give_the_same_bytes():
    bodies = [mesh_body('1 base #111111', '#111111', 1), mesh_body('2 #ff0000', '#ff0000', 2)]
    assert write_3mf(bodies) == write_3mf(bodies)


def test_3mf_needs_a_body():
    with pytest.raises(ValueError):
        write_3mf([])


def test_pad_3mf_names_parts_after_the_design_bodies(tmp_path):
    svg_path = tmp_path / 'art.svg'
    svg_path.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50">'
                        '<rect width="60" height="50" fill="red"/><rect x="60" width="40" height="50" fill="blue"/></svg>')
    design = build_pad(str(svg_path), PadSettings(multicolor=True), logger=lambda line: None)
    stls = {1: ascii_stl(TETRAHEDRON), 2: binary_stl(TETRAHEDRON)}

    files = unpack(pad_3mf(design, stls, title='art'))
    assert [mesh.get('name') for mesh in model_objects(files)] == ['1 base #ff0000', '2 #0000ff', 'art']
    with pytest.raises(ValueError, match='No STL for body'):
        pad_3mf(design, {1: stls[1]})
