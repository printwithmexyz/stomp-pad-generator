import math
import subprocess
import sys
import time

import numpy as np
import pytest
import shapely
from shapely.geometry import Point, Polygon, box

from stomppad import (
    CleanupSettings,
    Region,
    apply_overrides,
    clean_regions,
    merge_similar_fills,
    normalize_color,
    parse_svg,
    printability_warnings,
    scale_regions_to_mm,
)
from stomppad.overlay import (
    polygon_parts,
    robust_buffer,
    robust_difference,
    robust_intersection,
    robust_union,
)
from stomppad.svg_parse import UnsafeXmlError, parse_transform

SVG_NS = 'xmlns="http://www.w3.org/2000/svg"'
SNAP_NOISE = 1e-3  # mm^2; set operations snap to a grid a ten-millionth of the shape's size


@pytest.fixture
def svg(tmp_path):
    def write(body, root_attrs=f'{SVG_NS} viewBox="0 0 100 100"'):
        path = tmp_path / 'art.svg'
        path.write_text(f'<svg {root_attrs}>{body}</svg>')
        return str(path)
    return write


def total_area(parsed):
    return sum(region.geometry.area for region in parsed.regions)


# --- set operations that must never raise -----------------------------------

BOWTIE = Polygon([(0, 0), (2, 2), (2, 0), (0, 2)])


def test_plain_union_of_an_invalid_shape_raises():
    # Why the package never calls GEOS set operations directly: in Pyodide this
    # error is not a Python exception, it takes the whole runtime down.
    with pytest.raises(shapely.errors.GEOSException):
        BOWTIE.union(box(1, -1, 3, 3))


def test_robust_operations_accept_invalid_shapes():
    union = robust_union([BOWTIE, box(1, -1, 3, 3)])
    assert union.is_valid
    assert union.area == pytest.approx(8 + 1)
    assert robust_difference(BOWTIE, box(1, -1, 3, 3)).area == pytest.approx(1)
    assert robust_intersection(BOWTIE, box(1, -1, 3, 3)).area == pytest.approx(1)


def test_robust_operations_on_ordinary_shapes():
    assert robust_union([box(0, 0, 2, 2), box(1, 0, 3, 2)]).bounds == pytest.approx((0, 0, 3, 2))
    assert robust_difference(box(0, 0, 2, 2), box(1, 0, 3, 2)).area == pytest.approx(2)
    assert robust_intersection(box(0, 0, 2, 2), box(1, 0, 3, 2)).area == pytest.approx(2)
    assert robust_difference(box(0, 0, 2, 2), Polygon()).area == pytest.approx(4)


@pytest.mark.parametrize('shape', [
    box(0, 0, 20, 10),
    Point(0, 0).buffer(30).difference(Point(0, 0).buffer(12)),
    box(0, 0, 10, 10).union(box(30, 0, 40, 10)),
    box(0, 0, 30, 8).union(box(0, 0, 8, 30)),
])
@pytest.mark.parametrize('distance', [2.0, -2.0, -6.0])
@pytest.mark.parametrize('join_style', ['round', 'mitre'])
def test_robust_buffer_agrees_with_geos(shape, distance, join_style):
    ours = robust_buffer(shape, distance, join_style=join_style)
    theirs = shape.buffer(distance, join_style=join_style, quad_segs=32)
    assert ours.is_valid
    assert ours.symmetric_difference(theirs).area <= 0.004 * theirs.area + 0.05


def test_robust_buffer_keeps_holes_and_islands():
    ring_with_island = Point(0, 0).buffer(30).difference(Point(0, 0).buffer(20)).union(Point(0, 0).buffer(8))
    grown = robust_buffer(ring_with_island, 2)
    assert len(polygon_parts(grown)) == 2
    assert sum(len(polygon.interiors) for polygon in polygon_parts(grown)) == 1
    assert not grown.contains(Point(14, 0))
    assert robust_buffer(ring_with_island, -6).is_empty is False
    assert robust_buffer(ring_with_island, -9).is_empty


def test_robust_buffer_of_nothing_is_empty():
    assert robust_buffer(Polygon(), 2).is_empty
    assert robust_buffer(box(0, 0, 1, 1), -1).is_empty


def test_robust_buffer_far_from_the_origin_matches_the_same_shape_nearby():
    near = robust_buffer(box(0, 0, 20, 10), 2, join_style='mitre')
    far = robust_buffer(box(1e9, 1e9, 1e9 + 20, 1e9 + 10), 2, join_style='mitre')
    assert far.area == pytest.approx(near.area) == pytest.approx(24 * 14)
    assert far.bounds == pytest.approx((1e9 - 2, 1e9 - 2, 1e9 + 22, 1e9 + 12))


@pytest.mark.parametrize('bad', [math.nan, math.inf])
def test_robust_operations_treat_non_finite_shapes_as_nothing(bad):
    broken_line = shapely.LineString([(0, 0), (bad, 1), (5, 5)])
    broken_polygon = Polygon([(0, 0), (bad, 0), (1, 1)])
    assert robust_buffer(broken_line, 1).is_empty
    assert robust_buffer(box(0, 0, 1, 1), bad).is_empty
    assert robust_union([broken_polygon, box(0, 0, 2, 2)]).area == pytest.approx(4)
    assert robust_difference(box(0, 0, 2, 2), broken_polygon).area == pytest.approx(4)


def test_closed_flag_decides_whether_a_line_gets_a_join_or_caps():
    bowtie = shapely.LineString([(0, 0), (10, 10), (10, 0), (0, 10), (0, 0)])
    style = {'cap_style': 'flat', 'join_style': 'mitre'}
    joined = robust_buffer(bowtie, 1, closed=True, **style)
    capped = robust_buffer(bowtie, 1, closed=False, **style)
    start_corner = Point(-0.57, -1.56)
    assert joined.contains(start_corner) and not capped.contains(start_corner)
    assert robust_buffer(bowtie, 1, **style).area == pytest.approx(joined.area)


def test_offsets_survive_inputs_that_would_abort_clipper():
    # Clipper aborts the whole process outside its integer range, so a
    # regression here cannot be caught as an exception. Hence the subprocess.
    script = """
import tempfile
from shapely.geometry import LineString
from stomppad import parse_svg
from stomppad.overlay import robust_buffer
robust_buffer(LineString([(1e12, 1e12), (1e12 + 1, 1e12)]), 0.1)
robust_buffer(LineString([(1e15, 1e15), (1e15, 1e15)]), 1e-9)
for body in (
    '<path d="M50 50 L50 50" stroke="#000" stroke-width="1e-12" stroke-linecap="round" fill="none"/>',
    '<path d="M1e6 1e6 L1e6 1e6" stroke="#000" stroke-width="1e-9" stroke-linecap="round" fill="none"/>',
    '<rect width="5" height="5" stroke="black" stroke-width="1e999"/>',
):
    with tempfile.NamedTemporaryFile('w', suffix='.svg', delete=False) as handle:
        handle.write('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">' + body + '</svg>')
    parse_svg(handle.name)
print('survived')
"""
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'survived'


def test_union_of_nothing_is_empty():
    assert robust_union([]).is_empty
    assert robust_union([Polygon(), Polygon()]).is_empty


def test_polygon_parts_reaches_into_collections():
    nested = shapely.GeometryCollection([
        box(0, 0, 1, 1), shapely.LineString([(0, 0), (1, 1)]), Polygon(),
        shapely.MultiPolygon([box(2, 0, 3, 1), box(4, 0, 5, 1)]),
    ])
    assert len(polygon_parts(nested)) == 3
    assert polygon_parts(Polygon()) == []


# --- holes and fill rules ---------------------------------------------------

SQUARE_IN_SQUARE = 'M0 0 H100 V100 H0 Z M30 30 H70 V70 H30 Z'
SQUARE_IN_REVERSED_SQUARE = 'M0 0 H100 V100 H0 Z M30 30 V70 H70 V30 Z'


def test_evenodd_subpath_is_a_hole(svg):
    parsed = parse_svg(svg(f'<path fill-rule="evenodd" d="{SQUARE_IN_SQUARE}"/>'))
    assert len(parsed.regions) == 1
    assert len(parsed.regions[0].geometry.interiors) == 1
    assert total_area(parsed) == pytest.approx(8400)


def test_nonzero_same_direction_subpath_is_solid(svg):
    parsed = parse_svg(svg(f'<path d="{SQUARE_IN_SQUARE}"/>'))
    assert total_area(parsed) == pytest.approx(10000)


def test_nonzero_reversed_subpath_is_a_hole(svg):
    parsed = parse_svg(svg(f'<path d="{SQUARE_IN_REVERSED_SQUARE}"/>'))
    assert total_area(parsed) == pytest.approx(8400)


def test_curved_donut_keeps_its_hole(svg):
    donut = ('M50 5 A45 45 0 1 0 50 95 A45 45 0 1 0 50 5 Z '
             'M50 30 A20 20 0 1 0 50 70 A20 20 0 1 0 50 30 Z')
    parsed = parse_svg(svg(f'<path fill-rule="evenodd" d="{donut}"/>'), samples_per_segment=60)
    assert total_area(parsed) == pytest.approx(math.pi * (45 ** 2 - 20 ** 2), rel=0.01)
    assert not parsed.silhouette().contains(Point(50, 50))


# --- transforms -------------------------------------------------------------

def test_group_transform_moves_and_scales_shape(svg):
    parsed = parse_svg(svg(
        '<g transform="translate(100,0) scale(0.5)"><rect width="100" height="100"/></g>',
        f'{SVG_NS} viewBox="0 0 200 100"',
    ))
    assert parsed.regions[0].geometry.bounds == pytest.approx((100, 0, 150, 50))


def test_child_transform_applies_inside_parent_transform(svg):
    art = ('<g transform="translate(50,0)">'
           '<rect width="10" height="10" transform="scale(2)"/></g>')
    assert parse_svg(svg(art)).regions[0].geometry.bounds == pytest.approx((50, 0, 70, 20))


def test_matrix_and_skew_transforms():
    assert parse_transform('matrix(1 2 3 4 5 6)') @ np.array([1, 1, 1]) == pytest.approx([9, 12, 1])
    assert parse_transform('skewX(45)') @ np.array([0, 10, 1]) == pytest.approx([10, 10, 1])


def test_transforms_compose_left_to_right():
    matrix = parse_transform('translate(10,20) rotate(90)')
    assert matrix @ np.array([1, 0, 1]) == pytest.approx([10, 21, 1])


def test_rotate_about_a_point():
    matrix = parse_transform('rotate(180 50 50)')
    assert matrix @ np.array([0, 0, 1]) == pytest.approx([100, 100, 1])


# --- backgrounds and voids --------------------------------------------------

BACKGROUND = '<rect width="100" height="100" fill="#fff"/>'


def test_background_rect_is_dropped(svg):
    parsed = parse_svg(svg(BACKGROUND + '<circle cx="50" cy="50" r="30"/>'))
    assert parsed.background_fill == '#ffffff'
    assert parsed.fills() == ['#000000']
    assert total_area(parsed) == pytest.approx(math.pi * 900, rel=0.01)


def test_background_can_be_kept(svg):
    parsed = parse_svg(svg(BACKGROUND + '<circle cx="50" cy="50" r="30"/>'), keep_background=True)
    assert parsed.background_fill is None
    assert parsed.fills() == ['#ffffff', '#000000']
    assert total_area(parsed) == pytest.approx(10000)


def test_lone_full_canvas_shape_is_not_a_background(svg):
    parsed = parse_svg(svg('<rect width="100" height="100" fill="red"/>'))
    assert parsed.fills() == ['#ff0000']


def test_lone_shape_with_a_stroke_keeps_its_fill(svg):
    art = '<rect width="100" height="100" fill="red" stroke="black" stroke-width="4"/>'
    parsed = parse_svg(svg(art))
    assert parsed.background_fill is None
    assert parsed.fills() == ['#ff0000', '#000000']


def test_percent_sized_background_is_dropped(svg):
    art = '<rect width="100%" height="100%" fill="white"/><circle cx="250" cy="250" r="100"/>'
    parsed = parse_svg(svg(art, f'{SVG_NS} viewBox="0 0 500 500"'))
    assert parsed.background_fill == '#ffffff'
    assert parsed.fills() == ['#000000']


def test_background_with_offset_viewbox(svg):
    art = '<rect x="-50" y="-50" width="100" height="100" fill="#fff"/><circle r="20"/>'
    parsed = parse_svg(svg(art, f'{SVG_NS} viewBox="-50 -50 100 100"'))
    assert parsed.background_fill == '#ffffff'


def test_background_detected_from_width_and_height_with_units(svg):
    art = ('<rect width="60" height="60"/>'
           '<circle cx="150" cy="150" r="10" fill="red"/>')
    parsed = parse_svg(svg(art, f'{SVG_NS} width="50mm" height="50mm"'))
    assert parsed.background_fill is None
    assert parsed.viewbox == pytest.approx((0, 0, 188.976, 188.976), abs=0.001)


def test_rounded_badge_under_a_glyph_is_not_a_background(svg):
    art = ('<rect width="100" height="100" rx="10" fill="#e00"/>'
           '<circle cx="50" cy="50" r="20" fill="#fff"/>')
    parsed = parse_svg(svg(art))
    assert parsed.background_fill is None
    assert parsed.fills() == ['#ee0000', '#ffffff']


def test_dropped_background_is_reported(svg):
    parsed = parse_svg(svg(BACKGROUND + '<circle cx="50" cy="50" r="30"/>'))
    assert any('background' in warning for warning in parsed.warnings)


def test_art_matching_the_background_gets_a_clear_warning(svg):
    art = '<rect width="100" height="100"/><circle cx="50" cy="50" r="30"/>'
    parsed = parse_svg(svg(art))
    assert parsed.regions == []
    assert any('nothing is left' in warning for warning in parsed.warnings)


def test_background_colored_shape_cuts_a_hole(svg):
    smiley = (BACKGROUND
              + '<circle cx="50" cy="50" r="40" fill="#010102"/>'
              + '<circle cx="50" cy="50" r="30" fill="#ffffff"/>'
              + '<circle cx="40" cy="42" r="4" fill="#010102"/>'
              + '<circle cx="60" cy="42" r="4" fill="#010102"/>')
    parsed = parse_svg(svg(smiley))
    assert parsed.fills() == ['#010102']
    assert len(parsed.regions) == 3
    ring = max(parsed.regions, key=lambda region: region.geometry.area)
    assert len(ring.geometry.interiors) == 1
    assert not parsed.silhouette().contains(Point(50, 60))


@pytest.mark.parametrize('void_fills', [['red'], 'red', ('#f00',)])
def test_void_fill_cuts_a_hole(svg, void_fills):
    art = '<rect width="100" height="100"/><circle cx="50" cy="50" r="20" fill="red"/>'
    parsed = parse_svg(svg(art), keep_background=True, void_fills=void_fills)
    assert parsed.fills() == ['#000000']
    assert total_area(parsed) == pytest.approx(10000 - math.pi * 400, rel=0.01)


# --- colors and paint order -------------------------------------------------

def test_later_shape_paints_over_earlier(svg):
    art = ('<circle cx="50" cy="50" r="40" fill="red"/>'
           '<rect x="40" y="40" width="20" height="20" fill="blue"/>')
    parsed = parse_svg(svg(art))
    areas = {region.fill: region.geometry.area for region in parsed.regions}
    assert parsed.fills() == ['#ff0000', '#0000ff']
    assert areas['#0000ff'] == pytest.approx(400)
    assert areas['#ff0000'] == pytest.approx(math.pi * 1600 - 400, rel=0.01)


def test_fully_covered_shape_disappears(svg):
    art = ('<rect x="20" y="20" width="10" height="10"/>'
           '<rect x="10" y="10" width="50" height="50" fill="red"/>')
    assert parse_svg(svg(art)).fills() == ['#ff0000']


def test_disjoint_shapes_are_separate_regions(svg):
    art = ('<circle cx="50" cy="50" r="40" fill="#f00"/>'
           '<rect x="120" y="20" width="60" height="60" fill="#00f"/>')
    parsed = parse_svg(svg(art, f'{SVG_NS} viewBox="0 0 200 100"'))
    assert [region.fill for region in parsed.regions] == ['#ff0000', '#0000ff']
    assert total_area(parsed) == pytest.approx(math.pi * 1600 + 3600, rel=0.01)


def test_fill_from_stylesheet_class(svg):
    art = ('<defs><style>.cls-1{fill:#f00;} .cls-2, .cls-3 { fill: #00f }</style></defs>'
           '<rect class="cls-1" width="10" height="10"/>'
           '<rect class="cls-3" x="50" width="10" height="10"/>')
    assert parse_svg(svg(art)).fills() == ['#ff0000', '#0000ff']


def test_stylesheet_specificity_and_compound_selectors(svg):
    art = ('<style>#hero{fill:#00f} rect{fill:#0f0} .warm{fill:#f00} circle.warm{fill:#ff0}</style>'
           '<rect width="10" height="10" fill="black"/>'
           '<rect class="warm" x="20" width="10" height="10"/>'
           '<rect class="warm" id="hero" x="40" width="10" height="10"/>'
           '<circle class="warm" cx="70" cy="5" r="5"/>')
    parsed = parse_svg(svg(art))
    assert parsed.fills() == ['#00ff00', '#ff0000', '#0000ff', '#ffff00']
    assert parsed.warnings == []


def test_unsupported_selector_warns(svg):
    art = '<style>g > .deep{fill:#00f}</style><g><rect class="deep" width="10" height="10"/></g>'
    parsed = parse_svg(svg(art))
    assert parsed.fills() == ['#000000']
    assert any('CSS selectors' in warning for warning in parsed.warnings)


def test_inherit_keyword_takes_the_parent_value(svg):
    art = '<g fill="red"><rect width="10" height="10" fill="inherit"/></g>'
    assert parse_svg(svg(art)).fills() == ['#ff0000']


def test_gradient_and_current_color_fall_back_to_black(svg):
    art = ('<rect width="10" height="10" fill="url(#fade)"/>'
           '<rect x="20" width="10" height="10" fill="currentColor"/>')
    parsed = parse_svg(svg(art))
    assert parsed.fills() == ['#000000']
    assert any('Gradient' in warning for warning in parsed.warnings)


@pytest.mark.parametrize('invisible', [
    'fill="blue" opacity="0"',
    'fill="blue" fill-opacity="0"',
    'fill="rgba(0,0,255,0)"',
    'fill="#0000ff00"',
    'fill="white" style="opacity:0.01"',
])
def test_transparent_overlay_is_not_painted(svg, invisible):
    art = f'<rect width="50" height="50" fill="red"/><rect width="100" height="100" {invisible}/>'
    parsed = parse_svg(svg(art))
    assert [(region.fill, region.geometry.area) for region in parsed.regions] == [('#ff0000', 2500)]


def test_group_opacity_hides_children(svg):
    art = '<rect width="10" height="10" fill="red"/><g opacity="0"><rect width="100" height="100"/></g>'
    assert parse_svg(svg(art)).fills() == ['#ff0000']


def test_transparent_stroke_is_not_painted(svg):
    art = '<rect x="10" y="10" width="50" height="50" stroke="black" stroke-width="10" stroke-opacity="0"/>'
    assert total_area(parse_svg(svg(art))) == pytest.approx(2500)


def test_semi_transparent_shape_is_solid_with_a_warning(svg):
    parsed = parse_svg(svg('<rect width="10" height="10" fill="red" opacity="0.5"/>'))
    assert parsed.fills() == ['#ff0000']
    assert any('Semi-transparent' in warning for warning in parsed.warnings)


def test_inline_style_beats_attribute(svg):
    parsed = parse_svg(svg('<rect width="10" height="10" fill="red" style="fill:#00ff00"/>'))
    assert parsed.fills() == ['#00ff00']


def test_fill_inherits_from_group(svg):
    parsed = parse_svg(svg('<g fill="blue"><rect width="10" height="10"/></g>'))
    assert parsed.fills() == ['#0000ff']


@pytest.mark.parametrize('value, expected', [
    ('#F00', '#ff0000'),
    ('#ff000080', '#ff0000'),
    ('rgb(255, 128, 0)', '#ff8000'),
    ('rgb(100%, 0%, 0%)', '#ff0000'),
    ('White', '#ffffff'),
    ('none', None),
])
def test_normalize_color(value, expected):
    assert normalize_color(value) == expected


# --- element handling -------------------------------------------------------

def test_svg_without_namespace(svg):
    parsed = parse_svg(svg('<circle cx="50" cy="50" r="30"/>', 'viewBox="0 0 100 100"'))
    assert len(parsed.regions) == 1


def test_comma_separated_viewbox(svg):
    parsed = parse_svg(svg('<circle cx="50" cy="50" r="30"/>', f'{SVG_NS} viewBox="0,0,100,100"'))
    assert parsed.viewbox == (0, 0, 100, 100)


@pytest.mark.parametrize('hidden', [
    '<defs><rect x="50" width="10" height="10"/></defs>',
    '<g display="none"><rect x="50" width="10" height="10"/></g>',
    '<rect x="50" width="10" height="10" style="visibility:hidden"/>',
    '<g style="display: none"><rect x="50" width="10" height="10"/></g>',
])
def test_hidden_and_defs_shapes_are_skipped(svg, hidden):
    parsed = parse_svg(svg(hidden + '<rect width="10" height="10" fill="red"/>'))
    assert [(region.fill, region.geometry.area) for region in parsed.regions] == [('#ff0000', 100)]


def test_visible_child_of_hidden_group_shows(svg):
    art = '<g visibility="hidden"><rect width="10" height="10" visibility="visible"/></g>'
    assert total_area(parse_svg(svg(art))) == pytest.approx(100)


def test_stroke_only_line_becomes_a_bar(svg):
    art = '<path d="M10 50 L90 50" fill="none" stroke="black" stroke-width="10"/>'
    parsed = parse_svg(svg(art))
    assert parsed.regions[0].geometry.bounds == pytest.approx((10, 45, 90, 55))
    assert total_area(parsed) == pytest.approx(800)


def test_stroke_on_closed_shape_has_full_corners(svg):
    art = '<rect x="20" y="20" width="60" height="60" fill="none" stroke="black" stroke-width="4"/>'
    assert total_area(parse_svg(svg(art))) == pytest.approx(960)


def test_round_cap_and_join(svg):
    art = ('<path d="M10 50 L90 50" fill="none" stroke="black" stroke-width="10" '
           'stroke-linecap="round"/>')
    assert total_area(parse_svg(svg(art))) == pytest.approx(800 + math.pi * 25, rel=0.01)


def test_sharp_stroke_corner_is_bevelled_past_the_miter_limit(svg):
    spike = '<polyline points="40,90 50,10 60,90" fill="none" stroke="black" stroke-width="10" {}/>'
    default_limit = parse_svg(svg(spike.format('')))
    high_limit = parse_svg(svg(spike.format('stroke-miterlimit="20"')))
    assert default_limit.silhouette().bounds[1] > 4
    assert high_limit.silhouette().bounds[1] < -20


def test_square_stroke_corner_stays_mitred(svg):
    art = '<polyline points="10,10 50,10 50,50" fill="none" stroke="black" stroke-width="10"/>'
    assert parse_svg(svg(art)).silhouette().bounds == pytest.approx((10, 5, 55, 50))


@pytest.mark.parametrize('linecap, area', [('round', math.pi * 4), ('square', 16), ('butt', 0)])
def test_zero_length_stroke_is_a_dot_shaped_by_its_cap(svg, linecap, area):
    art = (f'<line x1="50" y1="50" x2="50" y2="50" stroke="black" stroke-width="4" '
           f'stroke-linecap="{linecap}"/><rect width="1" height="1" fill="red"/>')
    areas = {region.fill: region.geometry.area for region in parse_svg(svg(art)).regions}
    assert ('#000000' in areas) == (area > 0)
    assert areas.get('#000000', 0) == pytest.approx(area, rel=0.01)


def test_stroke_width_follows_transform(svg):
    art = '<line x1="10" y1="10" x2="40" y2="10" stroke="black" stroke-width="2" transform="scale(2)"/>'
    assert total_area(parse_svg(svg(art))) == pytest.approx(60 * 4)


def test_rounded_rect(svg):
    parsed = parse_svg(svg('<rect width="100" height="100" rx="10"/>'), samples_per_segment=60)
    assert total_area(parsed) == pytest.approx(10000 - (4 - math.pi) * 100, rel=0.001)


def test_ellipse_uses_both_radii(svg):
    parsed = parse_svg(svg('<ellipse cx="50" cy="50" rx="40" ry="10"/>'))
    assert parsed.regions[0].geometry.bounds == pytest.approx((10, 40, 90, 60), abs=0.1)


def test_lengths_with_units(svg):
    parsed = parse_svg(svg('<rect width="1in" height="25.4mm"/>', f'{SVG_NS} viewBox="0 0 200 200"'))
    assert parsed.regions[0].geometry.bounds == pytest.approx((0, 0, 96, 96))


def test_drawing_after_close_without_move_starts_a_new_subpath(svg):
    parsed = parse_svg(svg('<path fill-rule="evenodd" d="M0 0 H10 V10 Z L-10 0 L-10 -10 Z"/>'))
    assert total_area(parsed) == pytest.approx(100)


@pytest.mark.parametrize('broken', [
    '<g transform="scale(1e999)"><rect width="1" height="1" fill="#f00"/></g>',
    '<circle cx="0" cy="0" r="1e999" fill="#f00"/>',
    '<g transform="scale(1e200)"><rect width="1e200" height="1e200" fill="#f00"/></g>',
    '<rect x="1e12" width="5" height="5" fill="#f00"/>',
])
def test_out_of_range_shapes_are_skipped_with_a_warning(svg, broken):
    parsed = parse_svg(svg(broken + '<rect width="5" height="5"/>'))
    assert [(region.fill, region.geometry.area) for region in parsed.regions] == [('#000000', 25)]
    assert any('out-of-range' in warning for warning in parsed.warnings)


def test_invalid_path_is_skipped_with_a_warning(svg):
    art = '<path d="M0 0 L10 0 L10 10 L"/><path d="L10 0 L10 10 Z"/><rect width="5" height="5" fill="red"/>'
    parsed = parse_svg(svg(art))
    assert parsed.fills() == ['#ff0000']
    assert any('invalid data' in warning for warning in parsed.warnings)


def test_entity_declarations_give_a_clear_error_or_parse(tmp_path):
    path = tmp_path / 'illustrator.svg'
    path.write_text(
        '<?xml version="1.0"?>'
        '<!DOCTYPE svg [ <!ENTITY ns_svg "http://www.w3.org/2000/svg"> ]>'
        '<svg xmlns="&ns_svg;" viewBox="0 0 10 10"><rect width="5" height="5"/></svg>'
    )
    try:
        assert len(parse_svg(str(path)).regions) == 1
    except ValueError as error:
        assert isinstance(error.__cause__, UnsafeXmlError)
        assert 'plain SVG' in str(error)


def test_many_subpaths_stay_fast(svg):
    squares = ''.join(
        f'M{x} {y} h8 v8 h-8 Z M{x + 2} {y + 2} h4 v4 h-4 Z'
        for x in range(0, 400, 10) for y in range(0, 200, 10)
    )
    path = svg(f'<path fill-rule="evenodd" d="{squares}"/>', f'{SVG_NS} viewBox="0 0 400 200"')
    started = time.perf_counter()
    parsed = parse_svg(path)
    assert time.perf_counter() - started < 5
    assert total_area(parsed) == pytest.approx(800 * (64 - 16))


def test_many_overlapping_shapes_stay_fast(svg):
    rng = np.random.default_rng(7)
    circles = ''.join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}"/>'
        for (x, y), color in zip(rng.uniform(0, 400, (800, 2)), ['red', 'blue', 'lime', 'gold'] * 200)
    )
    started = time.perf_counter()
    parsed = parse_svg(svg(circles, f'{SVG_NS} viewBox="0 0 400 400"'))
    assert time.perf_counter() - started < 5
    assert len(parsed.fills()) == 4


def test_polyline_fill_closes_itself(svg):
    parsed = parse_svg(svg('<polyline points="0,0 10,0 10,10"/>'))
    assert total_area(parsed) == pytest.approx(50)


def test_text_produces_a_warning(svg):
    parsed = parse_svg(svg('<text x="10" y="10">Hi</text><rect width="10" height="10"/>'))
    assert any('outlines' in warning for warning in parsed.warnings)


def test_empty_svg_warns_instead_of_crashing(svg):
    parsed = parse_svg(svg(''))
    assert parsed.regions == []
    assert parsed.warnings == ['No filled shapes found in the SVG.']


# --- scaling ----------------------------------------------------------------

def test_scale_to_width_and_flip_y():
    top = Region(0, '#ff0000', box(10, 20, 60, 30))
    bottom = Region(1, '#0000ff', box(10, 30, 60, 40))
    scaled, scale = scale_regions_to_mm([top, bottom], target_width=100)
    assert scale == pytest.approx(2)
    assert scaled[0].geometry.bounds == pytest.approx((0, 20, 100, 40))
    assert scaled[1].geometry.bounds == pytest.approx((0, 0, 100, 20))


def test_scaled_exteriors_are_counter_clockwise():
    scaled, _ = scale_regions_to_mm([Region(0, '#000000', box(0, 0, 50, 20))], 100)
    assert scaled[0].geometry.exterior.is_ccw


def test_scaling_nothing_is_an_error():
    with pytest.raises(ValueError, match='No regions'):
        scale_regions_to_mm([], 100)


def test_scale_fits_inside_width_and_height():
    scaled, scale = scale_regions_to_mm([Region(0, '#000000', box(0, 0, 50, 20))], 100, 20)
    assert scale == pytest.approx(1)
    assert scaled[0].geometry.bounds == pytest.approx((0, 0, 50, 20))


# --- cleanup ----------------------------------------------------------------

def test_near_duplicate_fills_merge_into_the_larger():
    regions = [
        Region(0, '#000000', box(0, 0, 20, 20)),
        Region(1, '#010102', box(20, 0, 25, 20)),
        Region(2, '#ff0000', box(40, 0, 50, 10)),
    ]
    merged = merge_similar_fills(regions, tolerance=16)
    assert [region.fill for region in merged] == ['#000000', '#ff0000']
    assert merged[0].geometry.area == pytest.approx(500)


def test_cleanup_removes_spikes_specks_and_pinholes():
    square_with_pinhole = box(0, 0, 20, 20).difference(box(5, 5, 5.5, 5.5))
    spike = box(20, 10, 30, 10.1)
    speck = box(40, 40, 40.5, 40.5)
    regions = [
        Region(0, '#000000', square_with_pinhole.union(spike)),
        Region(1, '#000000', speck),
    ]
    cleaned, _ = clean_regions(regions)
    assert len(cleaned) == 1
    assert cleaned[0].geometry.bounds == pytest.approx((0, 0, 20, 20), abs=0.01)
    assert cleaned[0].geometry.area == pytest.approx(400, abs=0.01)
    assert len(cleaned[0].geometry.interiors) == 0


def test_merge_tolerance_boundary():
    regions = [Region(0, '#000000', box(0, 0, 20, 20)), Region(1, '#111111', box(30, 0, 40, 10))]
    assert len({r.fill for r in merge_similar_fills(regions, tolerance=17)}) == 1
    assert len({r.fill for r in merge_similar_fills(regions, tolerance=16)}) == 2


def test_cleanup_never_grows_a_shape():
    rounded_wedge = Point(0, 0).buffer(0.15).union(Point(30, 0).buffer(2.5)).convex_hull
    cleaned, _ = clean_regions([Region(0, '#000000', rounded_wedge)], CleanupSettings(simplify_tolerance=0))
    assert cleaned[0].geometry.difference(rounded_wedge).area < SNAP_NOISE


def test_cleaned_colors_do_not_overlap():
    inner = Region(0, '#ff0000', Point(0, 0).buffer(10, 64))
    outer = Region(1, '#0000ff', Point(0, 0).buffer(20, 16).difference(Point(0, 0).buffer(10, 64)))
    cleaned, _ = clean_regions([inner, outer], CleanupSettings(simplify_tolerance=0.3))
    red, blue = (region.geometry for region in cleaned)
    assert red.intersection(blue).area < SNAP_NOISE


def test_simplify_removes_stair_steps():
    stairs = [(0, 0), (20, 0)] + [(20 - i * 0.02, 10 + (i % 2) * 0.02) for i in range(1000)] + [(0, 10)]
    cleaned, _ = clean_regions([Region(0, '#000000', Polygon(stairs))])
    assert len(cleaned[0].geometry.exterior.coords) < 20


def test_cleanup_notes_merges_and_trims():
    regions = [
        Region(0, '#000000', box(0, 0, 20, 20).union(box(20, 5, 60, 5.1))),
        Region(1, '#010101', box(0, 30, 20, 50)),
    ]
    _, notes = clean_regions(regions)
    assert any(note.startswith('Merged 1') for note in notes)
    assert any(note.startswith('Trimmed 4.0') for note in notes)


def test_cleanup_keeps_sharp_tips():
    half_width = math.tan(math.radians(5)) * 20
    spike = Polygon([(-half_width, 0), (half_width, 0), (0, 20)]).union(box(-10, -10, 10, 0))
    cleaned, _ = clean_regions([Region(0, '#000000', spike)])
    assert cleaned[0].geometry.bounds[3] > 19


def test_cleanup_reports_colors_it_removed():
    regions = [Region(0, '#000000', box(0, 0, 20, 20)), Region(1, '#ff0000', box(30, 0, 30.2, 5))]
    cleaned, notes = clean_regions(regions)
    assert [region.fill for region in cleaned] == ['#000000']
    assert any('#ff0000' in note for note in notes)


def test_cleanup_keeps_printable_holes():
    donut = box(0, 0, 20, 20).difference(box(5, 5, 15, 15))
    cleaned, notes = clean_regions([Region(0, '#000000', donut)])
    assert cleaned[0].geometry.area == pytest.approx(300, abs=0.01)
    assert notes == []


def test_cleanup_settings_are_tunable():
    thin_bar = [Region(0, '#000000', box(0, 0, 20, 0.6))]
    assert len(clean_regions(thin_bar)[0]) == 1
    assert clean_regions(thin_bar, CleanupSettings(min_feature=0.8))[0] == []


# --- manual overrides -------------------------------------------------------

def test_override_drop_and_recolor():
    regions = [
        Region(0, '#ff0000', box(0, 0, 10, 10)),
        Region(1, '#0000ff', box(10, 0, 20, 10)),
        Region(2, '#00ff00', box(40, 0, 50, 10)),
    ]
    edited = apply_overrides(regions, drop_ids=[2], recolor={1: 'red'})
    assert len(edited) == 1
    assert edited[0].fill == '#ff0000'
    assert edited[0].geometry.area == pytest.approx(200)


def test_recolor_to_none_drops_the_region():
    regions = [Region(0, '#ff0000', box(0, 0, 10, 10)), Region(1, '#0000ff', box(20, 0, 30, 10))]
    edited = apply_overrides(regions, recolor={1: 'none'})
    assert [region.fill for region in edited] == ['#ff0000']
    assert len(clean_regions(edited)[0]) == 1


def test_overrides_ignore_unknown_ids():
    regions = [Region(0, '#ff0000', box(0, 0, 10, 10))]
    assert apply_overrides(regions, drop_ids=[9], recolor={9: 'blue'}) == regions


# --- printability -----------------------------------------------------------

def test_wide_region_has_no_warnings():
    assert printability_warnings([Region(0, '#000000', box(0, 0, 40, 40))], pyramid_size=4) == []


def test_narrow_region_cannot_hold_a_pyramid():
    warnings = printability_warnings([Region(7, '#000000', box(0, 0, 40, 2))], pyramid_size=4)
    assert [(w.code, w.region_id) for w in warnings] == [('no_grip', 7)]


def test_mostly_narrow_region_is_flagged():
    lollipop = box(0, 0, 20, 20).union(box(20, 9, 320, 11))
    warnings = printability_warnings([Region(0, '#000000', lollipop)], pyramid_size=4)
    assert [w.code for w in warnings] == ['partial_grip']


def test_safety_margin_tightens_the_fit():
    strip = [Region(0, '#000000', box(0, 0, 40, 4.5))]
    assert printability_warnings(strip, pyramid_size=4, safety_margin=0.1) == []
    assert [w.code for w in printability_warnings(strip, pyramid_size=4, safety_margin=0.5)] == ['no_grip']


def test_more_colors_than_filaments():
    fills = ['#ff0000', '#00ff00', '#0000ff', '#ffff00', '#ff00ff']
    regions = [Region(i, fill, box(i * 50, 0, i * 50 + 40, 40)) for i, fill in enumerate(fills)]
    assert [w.code for w in printability_warnings(regions, pyramid_size=4)] == ['too_many_colors']
