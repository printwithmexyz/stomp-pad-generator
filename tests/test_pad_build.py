import json
import math
import re
import shutil
import subprocess

import numpy as np
import pytest
import shapely
from shapely.geometry import Point, Polygon, box

from stomppad import packing
from stomppad.openscad import generate_openscad
from stomppad.packing import PackingSettings, calculate_skeleton, hex_grid, pyramid_footprints
from stomppad.pad import PadSettings, build_pad
from stomppad.svg_parse import polygon_parts

DONUT = ('<path fill-rule="evenodd" d="M50 5 A45 45 0 1 0 50 95 A45 45 0 1 0 50 5 Z '
         'M50 25 A25 25 0 1 0 50 75 A25 25 0 1 0 50 25 Z"/>')
SMILEY = ('<rect width="100" height="100" fill="#fff"/>'
          '<circle cx="50" cy="50" r="45"/>'
          '<circle cx="50" cy="50" r="30" fill="#fff"/>'
          '<circle cx="40" cy="42" r="6"/><circle cx="60" cy="42" r="6"/>')
THIN_DIAGONALS = ('<path d="M5 30 L95 5 L97 10.5 L7 36 Z"/>'
                  '<path d="M5 60 L95 85 L93.5 90.5 L3.5 65.5 Z"/>')


@pytest.fixture
def svg(tmp_path):
    def write(body, viewbox='0 0 100 100'):
        path = tmp_path / 'art.svg'
        path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{viewbox}">{body}</svg>')
        return str(path)
    return write


def build(svg_path, **overrides):
    return build_pad(svg_path, PadSettings(**overrides), logger=lambda message: None)


def scad_value(scad, name):
    return re.search(rf'^{name} = (.+);$', scad, re.MULTILINE).group(1)


def scad_module(scad, name):
    return re.search(rf'module {name}\(\) \{{\n(.*?)^\}}', scad, re.DOTALL | re.MULTILINE).group(1)


def scad_module_geometry(scad, name):
    """Rebuild a 2D module the way OpenSCAD reads it: the first path is the outline, the rest are holes."""
    polygons = []
    for points_text, paths_text in re.findall(
            r'polygon\(points = \[(.*?)\], paths = \[(.*?)\]\);', scad_module(scad, name)):
        points = np.array(re.findall(r'\[([-\d.]+), ([-\d.]+)\]', points_text), dtype=float)
        paths = [[int(i) for i in path.split(', ')] for path in re.findall(r'\[([\d, ]+)\]', paths_text)]
        polygons.append(Polygon(points[paths[0]], [points[path] for path in paths[1:]]))
    return shapely.union_all(polygons)


# --- voids ------------------------------------------------------------------

def test_hole_becomes_a_void_with_no_pyramids_in_it(svg):
    design = build(svg(DONUT), target_width=120)
    hole = Point(60, 60).buffer(25 * 120 / 90)
    assert design.voids.area == pytest.approx(hole.area, rel=0.01)
    positions = design.positions()
    assert len(positions) > 50
    assert not shapely.contains_xy(hole, positions[:, 0], positions[:, 1]).any()


def test_islands_inside_a_hole_are_not_void(svg):
    design = build(svg(SMILEY), target_width=90)
    left_eye = Point(35, 53)
    assert design.art.contains(left_eye)
    assert not design.voids.contains(left_eye)
    assert design.voids.contains(Point(45, 30))


def test_through_mode_writes_recess_off(svg):
    design = build(svg(DONUT))
    assert not design.voids_are_recessed
    assert scad_value(generate_openscad(design), 'recess_voids') == 'false'


def test_recess_mode_writes_toggle_and_depth(svg):
    design = build(svg(DONUT), void_mode='recess', recess_depth=0.8)
    scad = generate_openscad(design)
    assert design.voids_are_recessed
    assert scad_value(scad, 'recess_voids') == 'true'
    assert scad_value(scad, 'recess_depth') == '0.8'


def test_recess_as_deep_as_the_base_cuts_through(svg):
    design = build(svg(DONUT), void_mode='recess', recess_depth=2.0, base_thickness=2.0)
    assert not design.voids_are_recessed
    assert any('cut through' in warning for warning in design.warnings)


def test_narrow_hole_that_the_rim_fills_is_reported(svg):
    slotted = '<path fill-rule="evenodd" d="M0 0 H100 V100 H0 Z M20 48.5 H80 V51.5 H20 Z"/>'
    through = build(svg(slotted), target_width=100, outline_offset=2)
    recessed = build(svg(slotted), target_width=100, outline_offset=2, void_mode='recess')
    assert any('rim fills them in' in warning for warning in through.warnings)
    assert not any('rim fills them in' in warning for warning in recessed.warnings)


def test_loose_pieces_are_counted(svg):
    through = build(svg(SMILEY), target_width=90)
    recessed = build(svg(SMILEY), target_width=90, void_mode='recess')
    assert 'The pad prints as 3 separate pieces.' in through.warnings
    assert not any('separate pieces' in warning for warning in recessed.warnings)


@pytest.mark.parametrize('bad_settings', [
    {'void_mode': 'pocket'}, {'recess_depth': -1}, {'base_thickness': 0},
    {'target_width': -5}, {'outline_offset': -1},
])
def test_invalid_pad_settings_are_rejected(bad_settings):
    with pytest.raises(ValueError):
        PadSettings(**bad_settings)


@pytest.mark.parametrize('bad_settings', [
    {'pyramid_size': 0}, {'pyramid_style': 2}, {'safety_margin': -0.1}, {'skeleton_resolution': 0},
])
def test_invalid_packing_settings_are_rejected(bad_settings):
    with pytest.raises(ValueError):
        PackingSettings(**bad_settings)


def test_settings_from_form_values():
    settings = PadSettings.from_params({
        'target_width': 152, 'target_height': 0, 'pyramid_size': 3, 'pyramid_style': 6,
        'void_mode': 'recess', 'recess_depth': 0.4,
    })
    assert settings.target_height is None
    assert settings.packing == PackingSettings(pyramid_size=3, pyramid_style=6)
    assert (settings.void_mode, settings.recess_depth) == ('recess', 0.4)
    with pytest.raises(ValueError, match='Unknown settings'):
        PadSettings.from_params({'pyramid_sise': 3})


# --- sizing -----------------------------------------------------------------

def test_art_is_scaled_to_the_target_width(svg):
    design = build(svg(SMILEY), target_width=152)
    assert design.width == pytest.approx(152)
    assert design.art.bounds[:2] == pytest.approx((0, 0), abs=1e-6)


def test_width_holds_after_cleanup_trims_the_edge(svg):
    art = '<rect width="50" height="40"/><rect x="99.9" y="10" width="0.1" height="5"/>'
    design = build(svg(art), target_width=100)
    assert design.width == pytest.approx(100)
    assert len(design.regions) == 1


def test_art_sits_at_the_origin_after_cleanup_trims_the_corner(svg):
    art = '<rect x="10" width="90" height="80"/><rect y="99.9" width="0.1" height="0.1"/>'
    design = build(svg(art), target_width=100)
    assert design.art.bounds == pytest.approx((0, 0, 100, 100 * 80 / 90), abs=1e-6)


def test_height_limit_wins_when_tighter(svg):
    design = build(svg('<rect width="100" height="50"/>'), target_width=100, target_height=20)
    assert (design.width, design.height) == pytest.approx((40, 20))


def test_nothing_printable_is_an_error(svg):
    with pytest.raises(ValueError, match='No filled shapes'):
        build(svg(''))
    with pytest.raises(ValueError, match='too small to print'):
        build(svg('<rect width="100" height="0.2"/>'), target_width=20)


# --- colors -----------------------------------------------------------------

TWO_TONE = '<rect width="50" height="50" fill="red"/><rect x="50" width="50" height="50" fill="blue"/>'


def test_single_color_merges_fills_into_one_region(svg):
    design = build(svg(TWO_TONE))
    assert len(design.regions) == 1
    assert design.regions[0].geometry.area == pytest.approx(100 * 50)


def test_multicolor_keeps_fills_apart_on_one_floor(svg):
    design = build(svg(TWO_TONE), multicolor=True)
    assert [region.fill for region in design.regions] == ['#ff0000', '#0000ff']
    assert design.art.area == pytest.approx(100 * 50)
    assert set(design.packed) == {0, 1}


@pytest.mark.parametrize('gap, floors', [(0.3, 1), (0.5, 2)])
def test_hairline_gaps_between_shapes_are_bridged(svg, gap, floors):
    art = (f'<rect width="{50 - gap / 2}" height="40" fill="red"/>'
           f'<rect x="{50 + gap / 2}" width="{50 - gap / 2}" height="40" fill="blue"/>')
    design = build(svg(art), target_width=100, multicolor=True)
    assert len(polygon_parts(design.art)) == floors
    assert design.art.bounds == pytest.approx((0, 0, 100, 40), abs=1e-6)


# --- packing ----------------------------------------------------------------

def test_footprint_corners_match_the_scad_pyramid():
    square, hexagon = pyramid_footprints(np.array([[10.0, 20.0, 0.0], [0.0, 0.0, 15.0]]), 4)[0], \
        pyramid_footprints(np.array([[0.0, 0.0, 15.0]]), 4, sides=6)[0]
    corners = np.array(square.exterior.coords)[:-1] - (10, 20)
    assert np.degrees(np.arctan2(corners[:, 1], corners[:, 0])) % 360 == pytest.approx([45, 135, 225, 315])
    assert np.hypot(corners[:, 0], corners[:, 1]) == pytest.approx(4 / math.sqrt(2))
    assert square.area == pytest.approx(16)
    hexagon_corners = np.array(hexagon.exterior.coords)[:-1]
    assert len(hexagon_corners) == 6
    assert math.degrees(math.atan2(hexagon_corners[0, 1], hexagon_corners[0, 0])) == pytest.approx(60)


def test_centerline_angle_follows_the_skeleton():
    along = np.arange(0, 10, 0.25)
    skeleton = np.column_stack([along * math.cos(math.radians(30)), along * math.sin(math.radians(30))])
    centers = np.array([[4.0, 2.4], [500.0, 500.0]])
    assert packing._centerline_angles(centers, skeleton) == pytest.approx([30, 0])
    assert packing._centerline_angles(centers, skeleton[::-1]) == pytest.approx([30, 0])


def test_one_skeleton_point_is_not_a_direction():
    skeleton = np.array([[0.0, 0.0], [50.0, 50.0]])
    assert packing._centerline_angles(np.array([[0.5, 0.5]]), skeleton) == pytest.approx([0])


def test_skeleton_works_on_older_scikit_image(monkeypatch):
    seeds = []

    def medial_axis_before_rng(mask, random_state=None):
        seeds.append(random_state)
        return mask

    monkeypatch.setattr(packing, 'medial_axis', medial_axis_before_rng)
    assert len(calculate_skeleton(box(0, 0, 3, 3), 1.0)) > 0
    assert seeds == [0]


@pytest.mark.parametrize('safety_margin', [0.0, 0.5, 1.0])
def test_pyramids_keep_the_safety_margin(svg, safety_margin):
    design = build(svg(DONUT), target_width=120, packing=PackingSettings(safety_margin=safety_margin))
    footprints = pyramid_footprints(design.positions(), 4)
    safe_area = design.regions[0].geometry.buffer(-safety_margin + 1e-9)
    assert shapely.contains(safe_area, footprints).all()
    if safety_margin:
        tighter = build(svg(DONUT), target_width=120)
        looser = build(svg(DONUT), target_width=120, packing=PackingSettings(safety_margin=0))
        assert len(looser.positions()) > len(tighter.positions())


def test_six_sided_pyramids_are_checked_as_hexagons(svg):
    hexagons = PackingSettings(pyramid_style=6, safety_margin=0)
    design = build(svg('<rect width="100" height="14"/>'), target_width=100, packing=hexagons)
    footprints = pyramid_footprints(design.positions(), 4, sides=6)
    assert len(footprints) > 5
    assert shapely.contains(design.art.buffer(1e-9), footprints).all()


def test_rotations_are_folded_into_half_a_turn(svg):
    rotations = build(svg(THIN_DIAGONALS), target_width=120).positions()[:, 2]
    assert ((rotations >= 0) & (rotations < 180)).all()

@pytest.mark.parametrize('safety_margin', [0.5, 0.2])
def test_every_pyramid_base_fits_inside_the_art(svg, safety_margin):
    design = build(svg(THIN_DIAGONALS), target_width=120,
                   packing=PackingSettings(safety_margin=safety_margin))
    footprints = pyramid_footprints(design.positions(), 4)
    assert len(footprints) > 20
    assert shapely.contains(design.art, footprints).all()


def test_pyramids_turn_to_follow_a_diagonal_stroke(svg):
    design = build(svg('<path d="M5 30 L95 5 L97 10.5 L7 36 Z"/>'), target_width=120)
    stroke_angle = math.degrees(math.atan2(25, 90))  # rises to the right once Y points up
    rotations = design.positions()[:, 2] % 180
    assert np.median(rotations) == pytest.approx(stroke_angle, abs=3)


def test_rotation_can_be_turned_off(svg):
    design = build(svg(THIN_DIAGONALS), packing=PackingSettings(include_rotation=False))
    assert len(design.positions()) > 0
    assert not design.positions()[:, 2].any()


def test_separate_shapes_share_one_grid(svg):
    art = '<rect width="40" height="40"/><rect x="57" y="13" width="40" height="40"/>'
    design = build(svg(art), multicolor=True, packing=PackingSettings(include_rotation=False))
    row_height = 5 * math.sqrt(3) / 2
    rows = design.positions()[:, 1] / row_height
    assert all(len(packed.positions) > 10 for packed in design.packed.values())
    assert rows == pytest.approx(np.round(rows), abs=1e-6)


def test_skeleton_is_repeatable():
    crescent = Point(0, 0).buffer(30).difference(Point(5, 3).buffer(12))
    first = calculate_skeleton(crescent, 0.3)
    assert len(first) > 100
    assert all(np.array_equal(first, calculate_skeleton(crescent, 0.3)) for _ in range(5))


def test_hex_grid_spacing():
    grid = hex_grid((0, 0, 10, 10), pitch=5)
    first_row, second_row = grid[grid[:, 1] == 0], grid[np.isclose(grid[:, 1], 5 * math.sqrt(3) / 2)]
    assert np.diff(first_row[:, 0]) == pytest.approx(5)
    assert second_row[0, 0] - first_row[0, 0] == pytest.approx(2.5)
    assert grid[:, 0].max() >= 10 + 2.5 and grid[:, 1].max() >= 10
    assert box(0, 0, 10, 10).buffer(1e-9).contains(shapely.points(grid)).sum() == 8


def test_region_too_narrow_gets_no_pyramids_and_a_warning(svg):
    design = build(svg('<rect width="100" height="3"/>'), target_width=100)
    assert len(design.positions()) == 0
    assert any('too narrow' in warning for warning in design.warnings)
    assert 'pyramid_positions = [\n];' in generate_openscad(design)


# --- preview data -----------------------------------------------------------

def test_preview_is_plain_json_matching_the_design(svg):
    design = build(svg(SMILEY), target_width=90, void_mode='recess')
    preview = json.loads(json.dumps(design.preview()))
    rebuilt_art = shapely.union_all([Polygon(rings[0], rings[1:]) for rings in preview['art']])
    rebuilt_voids = shapely.union_all([Polygon(rings[0], rings[1:]) for rings in preview['voids']])
    assert rebuilt_art.symmetric_difference(design.art).area == pytest.approx(0, abs=1e-9)
    assert rebuilt_voids.symmetric_difference(design.voids).area == pytest.approx(0, abs=1e-9)
    assert rebuilt_voids.area > 100
    assert (preview['width'], preview['height']) == pytest.approx((design.width, design.height))
    assert preview['positions'] == design.positions().tolist()
    assert len(preview['skeleton']) == sum(len(p.skeleton) for p in design.packed.values())
    assert preview['voids_recessed'] is True
    assert (preview['pyramid_size'], preview['pyramid_sides']) == (4.0, 4)
    assert preview['warnings'] == design.warnings


# --- OpenSCAD output --------------------------------------------------------

def test_scad_carries_the_geometry_inline(svg):
    design = build(svg(DONUT), target_width=120)
    scad = generate_openscad(design)
    assert 'import(' not in scad
    art = scad_module(scad, 'art_2d')
    assert art.count('polygon(') == 1
    assert len(re.findall(r'\[\d+(?:, \d+)*\]', art.split('paths = ')[1])) == 2
    assert scad_module(scad, 'voids_2d').count('polygon(') == 1
    assert len(re.findall(r'^    \[[-\d., ]+\],$', scad, re.MULTILINE)) == len(design.positions())


@pytest.mark.parametrize('body', [DONUT, SMILEY, THIN_DIAGONALS])
def test_scad_polygons_rebuild_the_art_and_voids(svg, body):
    design = build(svg(body), target_width=120)
    scad = generate_openscad(design)
    for name, expected in (('art_2d', design.art), ('voids_2d', design.voids)):
        rebuilt = scad_module_geometry(scad, name)
        assert rebuilt.symmetric_difference(expected).area == pytest.approx(0, abs=0.05)
        assert rebuilt.area == pytest.approx(expected.area, rel=1e-4)


def test_scad_turns_pyramids_the_way_footprints_were_checked(svg):
    scad = generate_openscad(build(svg(DONUT)))
    assert 'rotate([0, 0, 45 + rotation])' in scad
    assert 'grip_pyramid(position[2]);' in scad


def test_scad_position_rows_keep_their_precision(svg):
    design = build(svg(THIN_DIAGONALS), target_width=120)
    rows = re.findall(r'^    \[([-\d.]+), ([-\d.]+), ([-\d.]+)\],$', generate_openscad(design), re.MULTILINE)
    assert np.array(rows, dtype=float) == pytest.approx(design.positions(), abs=0.0006)


def stl_volume(stl_path):
    vertices = np.array(
        re.findall(r'vertex\s+(\S+)\s+(\S+)\s+(\S+)', stl_path.read_text()), dtype=float,
    ).reshape(-1, 3, 3)
    return abs(np.einsum('ij,ij->i', vertices[:, 0], np.cross(vertices[:, 1], vertices[:, 2])).sum()) / 6


@pytest.mark.skipif(shutil.which('openscad') is None, reason='needs the OpenSCAD executable')
@pytest.mark.parametrize('void_mode', ['through', 'recess'])
def test_rendered_solid_has_the_expected_volume(svg, tmp_path, void_mode):
    design = build(svg(DONUT), target_width=40, void_mode=void_mode, recess_depth=0.8,
                   base_thickness=2, outline_offset=2, outline_height=0.8, pyramid_height=2.5)
    scad_path, stl_path = tmp_path / 'pad.scad', tmp_path / 'pad.stl'
    scad_path.write_text(generate_openscad(design))
    subprocess.run(['openscad', '-o', str(stl_path), str(scad_path)], check=True, capture_output=True)

    recessed = void_mode == 'recess'
    footprint = (design.art.union(design.voids) if recessed else design.art).buffer(2, 64)
    expected = footprint.area * 2.8 - design.art.area * 0.8 + len(design.positions()) * 16 * 2.5 / 3
    if recessed:
        expected -= design.voids.area * (0.8 + 0.8)
    assert len(design.positions()) > 3
    assert stl_volume(stl_path) == pytest.approx(expected, rel=0.01)


def test_scad_parameters_match_settings(svg):
    design = build(svg(DONUT), base_thickness=2.5, outline_offset=1.5, outline_height=2,
                   pyramid_height=3.5, packing=PackingSettings(pyramid_size=3, pyramid_style=6))
    scad = generate_openscad(design)
    values = {name: scad_value(scad, name) for name in (
        'base_thickness', 'outline_offset', 'outline_height', 'pyramid_size', 'pyramid_height', 'pyramid_style')}
    assert values == {
        'base_thickness': '2.5', 'outline_offset': '1.5', 'outline_height': '2',
        'pyramid_size': '3', 'pyramid_height': '3.5', 'pyramid_style': '6',
    }


def test_scad_without_voids_is_still_valid(svg):
    scad = generate_openscad(build(svg('<rect width="100" height="60"/>')))
    assert scad_module(scad, 'voids_2d') == ''
    assert scad.count('{') == scad.count('}')
