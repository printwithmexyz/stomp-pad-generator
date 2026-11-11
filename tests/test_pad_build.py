import math
import re

import numpy as np
import pytest
import shapely
from shapely.geometry import Point, box

from stomppad.openscad import generate_openscad
from stomppad.packing import PackingSettings, calculate_skeleton, hex_grid, pyramid_footprints
from stomppad.pad import PadSettings, build_pad

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


def test_invalid_void_settings_are_rejected():
    with pytest.raises(ValueError, match='void_mode'):
        PadSettings(void_mode='pocket')
    with pytest.raises(ValueError, match='negative'):
        PadSettings(recess_depth=-1)


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


# --- packing ----------------------------------------------------------------

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
    assert box(0, 0, 10, 10).buffer(1e-9).contains(shapely.points(grid)).sum() == 8


def test_region_too_narrow_gets_no_pyramids_and_a_warning(svg):
    design = build(svg('<rect width="100" height="3"/>'), target_width=100)
    assert len(design.positions()) == 0
    assert any('too narrow' in warning for warning in design.warnings)
    assert 'pyramid_positions = [\n];' in generate_openscad(design)


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


def test_scad_parameters_match_settings(svg):
    design = build(svg(DONUT), base_thickness=2.5, outline_offset=1.5, outline_height=2,
                   pyramid_height=3.5, pyramid_style=6, packing=PackingSettings(pyramid_size=3))
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
