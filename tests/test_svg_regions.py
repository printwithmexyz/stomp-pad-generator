import math

import numpy as np
import pytest
from shapely.geometry import Point, box

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
from stomppad.svg_parse import parse_transform

SVG_NS = 'xmlns="http://www.w3.org/2000/svg"'


@pytest.fixture
def svg(tmp_path):
    def write(body, root_attrs=f'{SVG_NS} viewBox="0 0 100 100"'):
        path = tmp_path / 'art.svg'
        path.write_text(f'<svg {root_attrs}>{body}</svg>')
        return str(path)
    return write


def total_area(parsed):
    return sum(region.geometry.area for region in parsed.regions)


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


def test_void_fill_cuts_a_hole(svg):
    art = '<rect width="100" height="100"/><circle cx="50" cy="50" r="20" fill="red"/>'
    parsed = parse_svg(svg(art), keep_background=True, void_fills=['red'])
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


def test_hidden_and_defs_shapes_are_skipped(svg):
    art = ('<defs><rect width="100" height="100"/></defs>'
           '<g display="none"><rect width="100" height="100"/></g>'
           '<rect width="100" height="100" style="visibility:hidden"/>'
           '<rect width="10" height="10" fill="red"/>')
    assert parse_svg(svg(art)).fills() == ['#ff0000']


def test_stroke_only_line_becomes_a_bar(svg):
    art = '<path d="M10 50 L90 50" fill="none" stroke="black" stroke-width="10"/>'
    parsed = parse_svg(svg(art))
    assert parsed.regions[0].geometry.bounds == pytest.approx((10, 45, 90, 55))
    assert total_area(parsed) == pytest.approx(800)


def test_stroke_width_follows_transform(svg):
    art = '<line x1="10" y1="10" x2="40" y2="10" stroke="black" stroke-width="2" transform="scale(2)"/>'
    assert total_area(parse_svg(svg(art))) == pytest.approx(60 * 4)


def test_rounded_rect(svg):
    parsed = parse_svg(svg('<rect width="100" height="100" rx="10"/>'), samples_per_segment=60)
    assert total_area(parsed) == pytest.approx(10000 - (4 - math.pi) * 100, rel=0.001)


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


def test_more_colors_than_filaments():
    fills = ['#ff0000', '#00ff00', '#0000ff', '#ffff00', '#ff00ff']
    regions = [Region(i, fill, box(i * 50, 0, i * 50 + 40, 40)) for i, fill in enumerate(fills)]
    assert [w.code for w in printability_warnings(regions, pyramid_size=4)] == ['too_many_colors']
