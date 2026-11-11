import json
import math
import pickle
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
from stomppad.pad import OUTPUT_STEP, PadSettings, _tidy, build_pad
from stomppad.overlay import polygon_parts, robust_union

DONUT = ('<path fill-rule="evenodd" d="M50 5 A45 45 0 1 0 50 95 A45 45 0 1 0 50 5 Z '
         'M50 25 A25 25 0 1 0 50 75 A25 25 0 1 0 50 25 Z"/>')
SMILEY = ('<rect width="100" height="100" fill="#fff"/>'
          '<circle cx="50" cy="50" r="45"/>'
          '<circle cx="50" cy="50" r="30" fill="#fff"/>'
          '<circle cx="40" cy="42" r="6"/><circle cx="60" cy="42" r="6"/>')
GRID_NOISE = 1e-4  # mm; offsets run on an integer grid a ten-millionth of the shape's size

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


def scad_positions(scad):
    """The pyramid rows as an (N, 4) array: x, y, rotation, body."""
    rows = re.findall(r'^    \[([-\d.]+), ([-\d.]+), ([-\d.]+), (\d+)\],$', scad, re.MULTILINE)
    return np.array(rows, dtype=float).reshape(-1, 4)


def scad_module_geometry(scad, name):
    """Rebuild a 2D module the way OpenSCAD reads it: the first path is the outline, the rest are holes."""
    polygons = []
    for points_text, paths_text in re.findall(
            r'polygon\(points = \[(.*?)\], paths = \[(.*?)\]\);', scad_module(scad, name)):
        points = np.array(re.findall(r'\[([-\d.]+), ([-\d.]+)\]', points_text), dtype=float)
        paths = [[int(i) for i in path.split(', ')] for path in re.findall(r'\[([\d, ]+)\]', paths_text)]
        polygons.append(Polygon(points[paths[0]], [points[path] for path in paths[1:]]))
    return robust_union(polygons)


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
    {'target_width': math.inf}, {'target_width': math.nan}, {'target_width': 1e300},
    {'target_width': 1e12}, {'target_height': -5}, {'target_height': math.inf},
    {'samples_per_segment': 1}, {'samples_per_segment': 0}, {'pyramid_height': math.inf},
    {'base_thickness': '2'}, {'outline_height': True},
])
def test_invalid_pad_settings_are_rejected(bad_settings):
    with pytest.raises(ValueError):
        PadSettings(**bad_settings)


@pytest.mark.parametrize('bad_settings', [
    {'pyramid_size': 0}, {'pyramid_style': 2}, {'safety_margin': -0.1}, {'skeleton_resolution': 0},
    {'pyramid_size': math.inf}, {'pyramid_spacing': math.nan}, {'pyramid_style': 1000},
])
def test_invalid_packing_settings_are_rejected(bad_settings):
    with pytest.raises(ValueError):
        PackingSettings(**bad_settings)


def test_settings_that_would_exhaust_memory_are_refused(svg):
    with pytest.raises(ValueError, match='too fine'):
        build(svg(DONUT), target_width=2000, packing=PackingSettings(skeleton_resolution=0.02))
    with pytest.raises(ValueError, match='too small for a pad this size'):
        build(svg(DONUT), target_width=2000, packing=PackingSettings(pyramid_size=0.2, pyramid_spacing=0))


def test_void_fills_have_one_canonical_order():
    assert PadSettings(void_fills={'#f00', '#00f', '#0f0'}).void_fills == ('#00f', '#0f0', '#f00')
    assert PadSettings(void_fills='red').void_fills == ('red',)
    assert repr(PadSettings(void_fills=['b', 'a'])) == repr(PadSettings(void_fills=('a', 'b')))


def test_settings_survive_a_trip_to_a_worker_process():
    settings = PadSettings.from_params({'target_width': 152, 'pyramid_size': 3, 'void_mode': 'recess'})
    assert pickle.loads(pickle.dumps(settings)) == settings


def test_settings_from_form_values():
    settings = PadSettings.from_params({
        'target_width': 152, 'target_height': 0, 'pyramid_size': 3, 'pyramid_style': 6,
        'void_mode': 'recess', 'recess_depth': 0.4,
    })
    assert settings.target_height is None
    assert PadSettings.from_params({'target_height': 80}).target_height == 80
    assert settings.packing == PackingSettings(pyramid_size=3, pyramid_style=6)
    assert (settings.void_mode, settings.recess_depth) == ('recess', 0.4)
    with pytest.raises(ValueError, match='Unknown settings'):
        PadSettings.from_params({'pyramid_sise': 3})


# --- sizing -----------------------------------------------------------------

def test_art_is_scaled_to_the_target_width(svg):
    design = build(svg(SMILEY), target_width=152)
    assert design.width == pytest.approx(152)
    assert design.art.bounds[:2] == pytest.approx((0, 0), abs=GRID_NOISE)


def test_width_holds_after_cleanup_trims_the_edge(svg):
    art = '<rect width="50" height="40"/><rect x="99.9" y="10" width="0.1" height="5"/>'
    design = build(svg(art), target_width=100)
    assert design.width == pytest.approx(100)
    assert len(design.regions) == 1


def test_art_sits_at_the_origin_after_cleanup_trims_the_corner(svg):
    art = '<rect x="10" width="90" height="80"/><rect y="99.9" width="0.1" height="0.1"/>'
    design = build(svg(art), target_width=100)
    assert design.art.bounds == pytest.approx((0, 0, 100, 100 * 80 / 90), abs=GRID_NOISE)


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
    assert design.art.bounds == pytest.approx((0, 0, 100, 40), abs=GRID_NOISE)


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
    assert rows == pytest.approx(np.round(rows), abs=GRID_NOISE)


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


# --- bodies -----------------------------------------------------------------

# A dark field with a red disc and a pale square that has a white disc inside it.
BADGE = ('<rect width="100" height="60" fill="#111111"/>'
         '<circle cx="30" cy="30" r="18" fill="#e63946"/>'
         '<rect x="60" y="15" width="28" height="30" fill="#9bd1e5"/>'
         '<circle cx="74" cy="30" r="6" fill="#ffffff"/>')
BADGE_SIZE = dict(viewbox='0 0 100 60')


def test_single_color_pad_is_one_body(svg):
    design = build(svg(TWO_TONE))
    assert [(body.number, body.fill, body.is_base) for body in design.bodies] == [(1, '#ff0000', True)]
    assert scad_value(generate_openscad(design), 'color_bodies') == '[]'
    assert not any('Filaments' in line for line in design.describe())


def test_largest_color_is_the_base_and_the_rest_follow_by_area(svg):
    design = build(svg(BADGE, **BADGE_SIZE), multicolor=True, keep_background=True)
    assert [(body.number, body.fill) for body in design.bodies] == [
        (1, '#111111'), (2, '#e63946'), (3, '#9bd1e5'), (4, '#ffffff'),
    ]
    assert design.bodies[0].top.is_empty
    assert design.bodies[1].top.area == pytest.approx(math.pi * 18 ** 2, rel=0.01)
    assert '  Filaments: 1 base #111111, 2 #e63946, 3 #9bd1e5, 4 #ffffff' in design.describe()


def test_a_base_color_no_region_uses_adds_a_body(svg):
    design = build(svg(TWO_TONE), multicolor=True, base_color='Black')
    assert [(body.number, body.fill) for body in design.bodies] == [
        (1, '#000000'), (2, '#0000ff'), (3, '#ff0000'),
    ]
    assert len(design.positions_by_body()[1]) == 0


def test_base_color_can_pick_a_smaller_color(svg):
    design = build(svg(BADGE, **BADGE_SIZE), multicolor=True, keep_background=True, base_color='#ffffff')
    assert [body.fill for body in design.bodies] == ['#ffffff', '#111111', '#e63946', '#9bd1e5']


def test_single_color_pad_with_a_base_color_is_two_tone(svg):
    design = build(svg(TWO_TONE), base_color='#222222')
    assert [body.fill for body in design.bodies] == ['#222222', '#ff0000']


def test_base_color_a_shade_off_an_art_color_means_that_color(svg):
    art = ('<rect width="60" height="50" fill="#000000"/><rect x="60" width="10" height="50" fill="#010102"/>'
           '<rect x="70" width="30" height="50" fill="#ff0000"/>')
    design = build(svg(art), multicolor=True, base_color='#020101')
    assert [body.fill for body in design.bodies] == ['#000000', '#ff0000']
    assert design.warnings == build(svg(art), multicolor=True).warnings


@pytest.mark.parametrize('bad_color', ['none', 'not-a-color'])
def test_base_color_must_be_a_color(bad_color):
    with pytest.raises(ValueError):
        PadSettings(base_color=bad_color)


def test_every_pyramid_belongs_to_the_body_it_stands_on(svg):
    design = build(svg(BADGE, **BADGE_SIZE), multicolor=True, keep_background=True)
    by_body = design.positions_by_body()
    assert sum(len(positions) for positions in by_body.values()) == len(design.positions())
    assert all(len(positions) > 0 for positions in by_body.values())
    packing = design.settings.packing
    for body in design.bodies[1:]:
        footprints = pyramid_footprints(by_body[body.number], packing.pyramid_size, packing.pyramid_style)
        assert shapely.contains(body.top.buffer(GRID_NOISE), footprints).all()

    rows = scad_positions(generate_openscad(design))
    for number, positions in by_body.items():
        assert rows[rows[:, 3] == number][:, :3] == pytest.approx(positions, abs=0.0006)


def test_color_tops_stay_on_the_floor_and_apart(svg):
    design = build(svg(BADGE, **BADGE_SIZE), multicolor=True, keep_background=True)
    tops = [body.top for body in design.bodies[1:]]
    for top in tops:
        assert top.difference(design.art).area < 0.01
    for index, top in enumerate(tops):
        for other in tops[index + 1:]:
            assert top.intersection(other).area < 0.01


def test_shapes_for_the_scad_sit_on_the_output_grid_with_no_specks(svg):
    design = build(svg(BADGE, **BADGE_SIZE), multicolor=True, keep_background=True)
    for geometry in [design.art, design.voids, *(body.top for body in design.bodies[1:])]:
        steps = shapely.get_coordinates(geometry) / OUTPUT_STEP
        assert steps == pytest.approx(np.round(steps), abs=1e-6)
        for polygon in polygon_parts(geometry):
            assert all(Polygon(ring).area > 0.01 for ring in [polygon.exterior, *polygon.interiors])


def test_tidy_clears_what_openscad_would_choke_on():
    plate = box(0, 0, 20, 10).difference(box(5, 5, 5.0003, 5.0003)).union(box(20, 4, 22, 4.0003))
    tidied = _tidy(plate)
    assert tidied.equals(box(0, 0, 20, 10))


def test_scad_has_one_color_branch_per_body_above_the_base(svg):
    design = build(svg(BADGE, **BADGE_SIZE), multicolor=True, keep_background=True, color_depth=0.4)
    scad = generate_openscad(design)
    assert scad_value(scad, 'part') == '0'
    assert scad_value(scad, 'color_depth') == '0.4'
    assert scad_value(scad, 'color_bodies') == '[2, 3, 4]'
    assert scad_value(scad, 'color_overlap') == '0.005'
    assert 'offset(delta = color_overlap) color_2d(body);' in scad
    color_module = re.search(r'module color_2d\(body\) \{\n(.*?)^\}', scad, re.DOTALL | re.MULTILINE).group(1)
    assert re.findall(r'if \(body == (\d+)\) \{', color_module) == ['2', '3', '4']
    assert '//   1 = #111111 (base' in scad and '//   4 = #ffffff' in scad
    assert scad.count('{') == scad.count('}')


def test_color_depth_is_capped_at_the_base_thickness(svg):
    design = build(svg(TWO_TONE), multicolor=True, base_thickness=1, color_depth=3)
    assert scad_value(generate_openscad(design), 'color_depth') == '1'
    assert any('run through the whole floor' in warning for warning in design.warnings)
    assert not any('run through' in w for w in build(svg(TWO_TONE), base_thickness=1, color_depth=3).warnings)


def test_a_speck_where_two_colors_meet_does_not_become_a_hole(svg):
    art = ('<rect width="50" height="60" fill="#e63946"/><rect x="50" width="50" height="60" fill="#1d3557"/>'
           '<rect x="49.6" y="30" width="0.8" height="0.9" fill="#ffffff"/>')
    design = build(svg(art, **BADGE_SIZE), multicolor=True)
    assert design.voids.is_empty
    assert sorted(body.fill for body in design.bodies) == ['#1d3557', '#e63946']
    assert sum(region.geometry.area for region in design.regions) == pytest.approx(6000, abs=0.05)


needs_openscad = pytest.mark.skipif(shutil.which('openscad') is None, reason='needs the OpenSCAD executable')


def render_part(scad_path, part):
    """Volume of one rendered part. Fails if OpenSCAD reports leaving anything out."""
    stl_path = scad_path.with_name(f'part_{part}.stl')
    result = subprocess.run(
        ['openscad', '-o', str(stl_path), '-D', f'part={part}', str(scad_path)],
        check=True, capture_output=True, text=True,
    )
    assert 'ERROR' not in result.stderr
    return stl_volume(stl_path)


@needs_openscad
@pytest.mark.parametrize('void_mode', ['through', 'recess'])
def test_rendered_bodies_add_up_to_the_whole_pad(svg, tmp_path, void_mode):
    art = BADGE + '<circle cx="45" cy="50" r="4" fill="#abcdef"/>'
    design = build(svg(art, **BADGE_SIZE), multicolor=True, keep_background=True, void_fills=('#abcdef',),
                   void_mode=void_mode, target_width=60)
    scad_path = tmp_path / 'pad.scad'
    scad_path.write_text(generate_openscad(design))

    assert design.voids.area > 10
    whole = render_part(scad_path, 0)
    volumes = {body.number: render_part(scad_path, body.number) for body in design.bodies}
    assert sum(volumes.values()) == pytest.approx(whole, rel=1e-4)

    pyramid = 16 * 2.5 / 3
    by_body = design.positions_by_body()
    for body in design.bodies[1:]:
        expected = body.top.area * 0.6 + len(by_body[body.number]) * pyramid
        assert volumes[body.number] == pytest.approx(expected, rel=0.01)


@needs_openscad
def test_colors_through_the_whole_floor_still_render(svg, tmp_path):
    design = build(svg(TWO_TONE), multicolor=True, target_width=40, base_thickness=1, color_depth=1)
    scad_path = tmp_path / 'pad.scad'
    scad_path.write_text(generate_openscad(design))
    whole = render_part(scad_path, 0)
    assert render_part(scad_path, 1) + render_part(scad_path, 2) == pytest.approx(whole, rel=1e-4)


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
    assert scad_positions(generate_openscad(design))[:, :3] == pytest.approx(design.positions(), abs=0.0006)


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
