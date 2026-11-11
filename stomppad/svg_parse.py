"""Turn an SVG into flat colored regions, resolved the way a browser paints it.

Later elements cover earlier ones, fill rules decide holes, and transforms and
CSS are applied. Geometry comes back in SVG user units with Y pointing down.
"""

from dataclasses import dataclass, field
import math
import re

import numpy as np
import shapely
from shapely.errors import GEOSException
from shapely.geometry import LineString, Polygon, box
from shapely.ops import polygonize, unary_union
from svg.path import parse_path

try:
    import defusedxml.ElementTree as ET
    from defusedxml.common import DefusedXmlException as UnsafeXmlError
except ImportError:
    import xml.etree.ElementTree as ET

    class UnsafeXmlError(Exception):
        pass


BACKGROUND_COVERAGE = 0.95
BACKGROUND_RECTANGULARITY = 0.999
MIN_VISIBLE_ALPHA = 0.05
FALLBACK_FILL = '#000000'
PIXELS_PER_UNIT = {
    '': 1.0, 'px': 1.0, 'in': 96.0, 'cm': 96 / 2.54, 'mm': 96 / 25.4, 'pt': 96 / 72, 'pc': 16.0,
}

SHAPE_TAGS = {'path', 'rect', 'circle', 'ellipse', 'polygon', 'polyline', 'line'}
NON_RENDERED_TAGS = {
    'defs', 'clipPath', 'mask', 'symbol', 'pattern', 'marker', 'filter',
    'linearGradient', 'radialGradient', 'metadata', 'title', 'desc', 'style',
}
UNSUPPORTED_TAGS = {
    'text': 'Text is ignored. Convert it to outlines before exporting the SVG.',
    'image': 'Embedded raster images are ignored. Trace them to vector shapes first.',
    'use': '<use> clones are ignored. Flatten or unlink clones before exporting.',
}

INHERITED_PROPERTIES = (
    'fill', 'fill-rule', 'fill-opacity', 'stroke', 'stroke-width', 'stroke-opacity',
    'stroke-linecap', 'stroke-linejoin', 'stroke-miterlimit', 'visibility',
)
STYLE_PROPERTIES = INHERITED_PROPERTIES + ('display', 'opacity')
ROOT_STYLE = {
    'fill': 'black', 'fill-rule': 'nonzero', 'fill-opacity': '1',
    'stroke': 'none', 'stroke-width': '1', 'stroke-opacity': '1',
    'stroke-linecap': 'butt', 'stroke-linejoin': 'miter', 'stroke-miterlimit': '4',
    'visibility': 'visible',
}

CAP_STYLES = {'butt': 'flat', 'round': 'round', 'square': 'square'}
JOIN_STYLES = {'miter': 'mitre', 'round': 'round', 'bevel': 'bevel'}

NAMED_COLORS = {
    'black': '#000000', 'white': '#ffffff', 'red': '#ff0000', 'lime': '#00ff00',
    'green': '#008000', 'blue': '#0000ff', 'yellow': '#ffff00', 'cyan': '#00ffff',
    'aqua': '#00ffff', 'magenta': '#ff00ff', 'fuchsia': '#ff00ff', 'gray': '#808080',
    'grey': '#808080', 'silver': '#c0c0c0', 'maroon': '#800000', 'olive': '#808000',
    'navy': '#000080', 'purple': '#800080', 'teal': '#008080', 'orange': '#ffa500',
    'pink': '#ffc0cb', 'brown': '#a52a2a', 'gold': '#ffd700',
}

_NUMBER = r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?'
_NUMBER_RE = re.compile(_NUMBER)
_LENGTH_RE = re.compile(r'\s*(' + _NUMBER + r')\s*([a-zA-Z%]*)')
_TRANSFORM_RE = re.compile(r'(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)')
_CSS_COMMENT_RE = re.compile(r'/\*.*?\*/', re.DOTALL)
_CSS_RULE_RE = re.compile(r'([^{}]+)\{([^{}]*)\}')
_COMPOUND_SELECTOR_RE = re.compile(r'(\*|[a-zA-Z][\w-]*)?((?:[.#][\w-]+)*)')


@dataclass(frozen=True)
class Region:
    """One connected piece of a single fill color, holes preserved."""
    id: int
    fill: str
    geometry: Polygon


@dataclass
class ParsedSvg:
    regions: list
    viewbox: tuple = None
    background_fill: str = None
    warnings: list = field(default_factory=list)

    def fills(self):
        return list(dict.fromkeys(region.fill for region in self.regions))

    def silhouette(self):
        return robust_union([region.geometry for region in self.regions])


@dataclass
class _Subpath:
    points: np.ndarray
    closed: bool


@dataclass
class _Paint:
    geometry: object
    fill: str
    element: int


@dataclass
class _CssRule:
    specificity: tuple
    tag: str
    classes: frozenset
    element_id: str
    declarations: dict


@dataclass
class _WalkContext:
    samples: int
    viewport: tuple
    css_rules: list = field(default_factory=list)
    paint: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    shapes_seen: int = 0

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)


def _snap_grid(geometries):
    min_x, min_y, max_x, max_y = shapely.total_bounds(geometries)
    return max(max_x - min_x, max_y - min_y) * 1e-7


def robust_union(geometries):
    """Union that survives GEOS topology errors by retrying on a fine snap grid."""
    geometries = list(geometries)
    try:
        return unary_union(geometries)
    except GEOSException:
        valid = [shapely.make_valid(g) for g in geometries]
        return shapely.union_all(valid, grid_size=_snap_grid(valid))


def robust_difference(geometry, cutter):
    try:
        return geometry.difference(cutter)
    except GEOSException:
        valid = [shapely.make_valid(geometry), shapely.make_valid(cutter)]
        return shapely.difference(*valid, grid_size=_snap_grid(valid))


def _local_name(tag):
    return tag.rsplit('}', 1)[-1] if isinstance(tag, str) else ''


def _numbers(text):
    return [float(n) for n in _NUMBER_RE.findall(text or '')]


def _length(value, percent_of=0.0, default=0.0):
    """Length in user units. Percentages resolve against percent_of."""
    found = _LENGTH_RE.match(value or '')
    if not found:
        return default
    number, unit = float(found.group(1)), found.group(2).lower()
    if unit == '%':
        return number * percent_of / 100
    return number * PIXELS_PER_UNIT.get(unit, 1.0)


def _fraction(value):
    found = _LENGTH_RE.match(value or '')
    if not found:
        return 1.0
    number = float(found.group(1)) / (100 if found.group(2) == '%' else 1)
    return max(0.0, min(1.0, number))


def _parse_color(value):
    """Return ('#rrggbb', alpha), (None, 0.0) for no paint, or raise ValueError."""
    text = value.strip().lower()
    if text in ('none', 'transparent'):
        return None, 0.0
    if text in NAMED_COLORS:
        return NAMED_COLORS[text], 1.0
    hex_digits = re.fullmatch(r'#([0-9a-f]+)', text)
    if hex_digits:
        digits = hex_digits.group(1)
        if len(digits) in (3, 4):
            digits = ''.join(c * 2 for c in digits)
        if len(digits) in (6, 8):
            alpha = int(digits[6:8], 16) / 255 if len(digits) == 8 else 1.0
            return '#' + digits[:6], alpha
    functional = re.fullmatch(r'rgba?\(([^)]*)\)', text)
    if functional:
        parts = [p for p in re.split(r'[\s,/]+', functional.group(1).strip()) if p]
        if len(parts) in (3, 4):
            channels = [
                float(p[:-1]) * 2.55 if p.endswith('%') else float(p) for p in parts[:3]
            ]
            color = '#%02x%02x%02x' % tuple(max(0, min(255, round(c))) for c in channels)
            return color, _fraction(parts[3]) if len(parts) == 4 else 1.0
    raise ValueError(f'Unrecognized color: {value}')


def normalize_color(value):
    """Return '#rrggbb', None for no paint, or raise ValueError if unrecognized."""
    return _parse_color(value)[0]


def _resolve_paint(value, ctx):
    text = value.strip()
    if text.lower() == 'currentcolor':
        return FALLBACK_FILL, 1.0
    if text.lower().startswith('url('):
        ctx.warn('Gradient and pattern fills are flattened to black.')
        return FALLBACK_FILL, 1.0
    try:
        return _parse_color(text)
    except ValueError:
        ctx.warn(f'Unrecognized color "{text}" treated as black.')
        return FALLBACK_FILL, 1.0


def _is_painted(color, alpha, ctx):
    if color is None or alpha <= MIN_VISIBLE_ALPHA:
        return False
    if alpha < 1:
        ctx.warn('Semi-transparent shapes are treated as solid.')
    return True


def _single_transform(name, values):
    def get(index, default):
        return values[index] if len(values) > index else default

    if name == 'matrix' and len(values) == 6:
        a, b, c, d, e, f = values
        return np.array([[a, c, e], [b, d, f], [0, 0, 1]], dtype=float)
    if name == 'translate':
        return np.array([[1, 0, get(0, 0)], [0, 1, get(1, 0)], [0, 0, 1]], dtype=float)
    if name == 'scale':
        sx = get(0, 1)
        return np.diag([sx, get(1, sx), 1.0])
    if name == 'rotate':
        angle = math.radians(get(0, 0))
        cos, sin = math.cos(angle), math.sin(angle)
        cx, cy = get(1, 0), get(2, 0)
        return np.array([
            [cos, -sin, cx - cos * cx + sin * cy],
            [sin, cos, cy - sin * cx - cos * cy],
            [0, 0, 1],
        ])
    if name == 'skewX':
        return np.array([[1, math.tan(math.radians(get(0, 0))), 0], [0, 1, 0], [0, 0, 1]])
    if name == 'skewY':
        return np.array([[1, 0, 0], [math.tan(math.radians(get(0, 0))), 1, 0], [0, 0, 1]])
    return np.identity(3)


def parse_transform(text):
    matrix = np.identity(3)
    for name, args in _TRANSFORM_RE.findall(text or ''):
        matrix = matrix @ _single_transform(name, _numbers(args))
    return matrix


def _apply_transform(points, matrix):
    return points @ matrix[:2, :2].T + matrix[:2, 2]


def _parse_declarations(text):
    declarations = {}
    for chunk in (text or '').split(';'):
        name, separator, value = chunk.partition(':')
        if separator:
            declarations[name.strip().lower()] = value.replace('!important', '').strip()
    return declarations


def _parse_selector(selector, declarations):
    """A rule for tag, .class, #id and compounds like tag.class; None for anything else."""
    found = _COMPOUND_SELECTOR_RE.fullmatch(selector)
    if not selector or not found:
        return None
    qualifiers = re.findall(r'[.#][\w-]+', found.group(2))
    classes = frozenset(q[1:] for q in qualifiers if q[0] == '.')
    ids = [q[1:] for q in qualifiers if q[0] == '#']
    tag = found.group(1) if found.group(1) not in (None, '*') else None
    specificity = (len(ids), len(classes), 1 if tag else 0)
    return _CssRule(specificity, tag, classes, ids[0] if ids else None, declarations)


def _parse_stylesheet(root, ctx):
    rules = []
    for element in root.iter():
        if _local_name(element.tag) != 'style' or not element.text:
            continue
        css = _CSS_COMMENT_RE.sub('', element.text)
        for selectors, body in _CSS_RULE_RE.findall(css):
            declarations = _parse_declarations(body)
            for selector in selectors.split(','):
                rule = _parse_selector(selector.strip(), declarations)
                if rule is not None:
                    rules.append(rule)
                elif not selector.strip().startswith('@'):
                    ctx.warn('Some CSS selectors were not understood. '
                             'Shapes styled by them may get the wrong color.')
    return sorted(rules, key=lambda rule: rule.specificity)


def _matching_css(element, tag, rules):
    """Declarations from matching rules, least specific first."""
    classes = set((element.get('class') or '').split())
    element_id = element.get('id')
    return [
        rule.declarations for rule in rules
        if rule.tag in (None, tag)
        and rule.classes <= classes
        and rule.element_id in (None, element_id)
    ]


def _resolve_style(element, tag, inherited, rules):
    """Return (inherited style, the element's own declarations): attributes < stylesheet < inline."""
    own = {name: element.get(name) for name in STYLE_PROPERTIES if element.get(name)}
    for declarations in _matching_css(element, tag, rules):
        own.update(declarations)
    own.update(_parse_declarations(element.get('style')))

    style = dict(inherited)
    for name in INHERITED_PROPERTIES:
        if own.get(name, 'inherit') != 'inherit':
            style[name] = own[name]
    return style, own


def _path_subpaths(d, ctx):
    try:
        segments = list(parse_path(d))
    except (ValueError, AssertionError, IndexError):
        ctx.warn('A path with invalid data was skipped.')
        return []

    subpaths = []
    points, closed = [], False

    def flush():
        if len(points) >= 2:
            subpaths.append(_Subpath(np.array(points, dtype=float), closed))

    for segment in segments:
        kind = type(segment).__name__
        if kind == 'Move' or closed:
            flush()
            start = segment.end if kind == 'Move' else segment.start
            points, closed = [(start.real, start.imag)], False
            if kind == 'Move':
                continue
        if kind not in ('Line', 'Close'):
            for step in range(1, ctx.samples):
                sample = segment.point(step / ctx.samples)
                points.append((sample.real, sample.imag))
        points.append((segment.end.real, segment.end.imag))
        closed = kind == 'Close'
    flush()
    return subpaths


def _ellipse_points(cx, cy, rx, ry, samples):
    count = max(samples * 4, 64)
    angles = np.linspace(0, 2 * np.pi, count, endpoint=False)
    return np.column_stack([cx + rx * np.cos(angles), cy + ry * np.sin(angles)])


def _rect_points(x, y, width, height, rx, ry, samples):
    rx, ry = (ry if rx < 0 else rx), (rx if ry < 0 else ry)
    rx, ry = min(max(rx, 0.0), width / 2), min(max(ry, 0.0), height / 2)
    if rx == 0 or ry == 0:
        return np.array([(x, y), (x + width, y), (x + width, y + height), (x, y + height)])

    corners = [
        (x + width - rx, y + ry, -90), (x + width - rx, y + height - ry, 0),
        (x + rx, y + height - ry, 90), (x + rx, y + ry, 180),
    ]
    arc_steps = max(samples // 2, 4)
    points = []
    for cx, cy, start_angle in corners:
        for angle in np.radians(np.linspace(start_angle, start_angle + 90, arc_steps + 1)):
            points.append((cx + rx * math.cos(angle), cy + ry * math.sin(angle)))
    return np.array(points)


def _element_subpaths(element, tag, ctx):
    view_width, view_height = ctx.viewport

    def across(name, default=0.0):
        return _length(element.get(name), view_width, default)

    def down(name, default=0.0):
        return _length(element.get(name), view_height, default)

    if tag == 'path':
        return _path_subpaths(element.get('d') or '', ctx)
    if tag == 'rect':
        width, height = across('width'), down('height')
        if width <= 0 or height <= 0:
            return []
        points = _rect_points(
            across('x'), down('y'), width, height, across('rx', -1.0), down('ry', -1.0), ctx.samples,
        )
        return [_Subpath(points, True)]
    if tag == 'circle':
        radius = _length(element.get('r'), math.hypot(view_width, view_height) / math.sqrt(2))
        if radius <= 0:
            return []
        return [_Subpath(_ellipse_points(across('cx'), down('cy'), radius, radius, ctx.samples), True)]
    if tag == 'ellipse':
        rx, ry = across('rx'), down('ry')
        if rx <= 0 or ry <= 0:
            return []
        return [_Subpath(_ellipse_points(across('cx'), down('cy'), rx, ry, ctx.samples), True)]
    if tag in ('polygon', 'polyline'):
        values = _numbers(element.get('points'))
        points = np.array(values[:len(values) // 2 * 2], dtype=float).reshape(-1, 2)
        return [_Subpath(points, tag == 'polygon')] if len(points) >= 2 else []
    return [_Subpath(np.array([(across('x1'), down('y1')), (across('x2'), down('y2'))]), False)]


def _closed_ring(points):
    if np.array_equal(points[0], points[-1]):
        return points
    return np.vstack([points, points[:1]])


def _ring_edges(rings):
    """Every edge of every ring as four flat arrays, so one pass tests them all."""
    starts = np.vstack([ring[:-1] for ring in rings])
    ends = np.vstack([ring[1:] for ring in rings])
    return starts[:, 0], starts[:, 1], ends[:, 0], ends[:, 1]


def _is_filled(point, edges, fill_rule):
    """Cast a +x ray from the point and apply the fill rule to the edges it crosses."""
    x0, y0, x1, y1 = edges
    side = (x1 - x0) * (point.y - y0) - (point.x - x0) * (y1 - y0)
    upward = int(np.count_nonzero((y0 <= point.y) & (y1 > point.y) & (side > 0)))
    downward = int(np.count_nonzero((y0 > point.y) & (y1 <= point.y) & (side < 0)))
    if fill_rule == 'evenodd':
        return (upward + downward) % 2 == 1
    return upward != downward


def _fill_geometry(subpaths, fill_rule):
    """Area a browser would fill, honoring the fill rule across subpaths."""
    rings = [_closed_ring(s.points) for s in subpaths if len(s.points) >= 3]
    if not rings:
        return None
    if len(rings) == 1:
        simple = Polygon(rings[0])
        if simple.is_valid:
            return simple
    edges = _ring_edges(rings)
    faces = polygonize(unary_union([LineString(ring) for ring in rings]))
    filled = [f for f in faces if _is_filled(f.representative_point(), edges, fill_rule)]
    return robust_union(filled) if filled else None


def _has_corner_past_miter_limit(points, closed, limit):
    """SVG bevels any corner whose miter would be longer than limit * stroke width."""
    steps = np.diff(points, axis=0)
    lengths = np.linalg.norm(steps, axis=1)
    directions = steps[lengths > 0] / lengths[lengths > 0, None]
    if closed and len(directions):
        directions = np.vstack([directions, directions[:1]])
    if len(directions) < 2:
        return False
    cos_turn = np.einsum('ij,ij->i', directions[:-1], directions[1:])
    sin_half_corner = np.sqrt(np.clip((1 + cos_turn) / 2, 0, 1))
    return bool(np.any(sin_half_corner * limit < 1))


def _stroke_geometry(subpaths, width, style):
    cap = CAP_STYLES.get(style['stroke-linecap'].strip(), 'flat')
    join = JOIN_STYLES.get(style['stroke-linejoin'].strip(), 'mitre')
    miter_limit = max(_length(style['stroke-miterlimit'], default=4.0), 1.0)
    outlines = []
    for subpath in subpaths:
        points = _closed_ring(subpath.points) if subpath.closed else subpath.points
        sharp = join == 'mitre' and _has_corner_past_miter_limit(points, subpath.closed, miter_limit)
        outlines.append(LineString(points).buffer(
            width / 2, cap_style=cap, join_style='bevel' if sharp else join, mitre_limit=miter_limit,
        ))
    return robust_union(outlines)


def _paint_shape(element, tag, matrix, style, alpha, ctx):
    subpaths = _element_subpaths(element, tag, ctx)
    for subpath in subpaths:
        subpath.points = _apply_transform(subpath.points, matrix)
    if not subpaths:
        return
    ctx.shapes_seen += 1

    fill, fill_alpha = _resolve_paint(style['fill'], ctx)
    if _is_painted(fill, alpha * fill_alpha * _fraction(style['fill-opacity']), ctx):
        filled = _fill_geometry(subpaths, style['fill-rule'].strip().lower())
        if filled is not None and not filled.is_empty:
            ctx.paint.append(_Paint(filled, fill, ctx.shapes_seen))

    stroke, stroke_alpha = _resolve_paint(style['stroke'], ctx)
    view_diagonal = math.hypot(*ctx.viewport) / math.sqrt(2)
    stroke_width = (_length(style['stroke-width'], view_diagonal, 1.0)
                    * math.sqrt(abs(np.linalg.det(matrix[:2, :2]))))
    if stroke_width > 0 and _is_painted(
            stroke, alpha * stroke_alpha * _fraction(style['stroke-opacity']), ctx):
        ctx.paint.append(_Paint(_stroke_geometry(subpaths, stroke_width, style), stroke, ctx.shapes_seen))


def _collect_paint(element, matrix, inherited, alpha, ctx):
    tag = _local_name(element.tag)
    if tag in NON_RENDERED_TAGS:
        return
    if tag in UNSUPPORTED_TAGS:
        ctx.warn(UNSUPPORTED_TAGS[tag])
        return

    style, own = _resolve_style(element, tag, inherited, ctx.css_rules)
    if own.get('display', '').strip() == 'none':
        return
    if element.get('clip-path') or element.get('mask'):
        ctx.warn('Clip paths and masks are ignored. Shapes are used unclipped.')

    alpha *= _fraction(own.get('opacity'))
    matrix = matrix @ parse_transform(element.get('transform'))
    if tag in SHAPE_TAGS:
        if style['visibility'].strip() == 'visible':
            _paint_shape(element, tag, matrix, style, alpha, ctx)
        return
    for child in element:
        _collect_paint(child, matrix, style, alpha, ctx)


def _parse_viewbox(root):
    values = _numbers(root.get('viewBox'))
    if len(values) == 4 and values[2] > 0 and values[3] > 0:
        return tuple(values)
    width, height = root.get('width') or '', root.get('height') or ''
    if '%' not in width + height and _length(width) > 0 and _length(height) > 0:
        return (0.0, 0.0, _length(width), _length(height))
    return None


def _is_canvas_rectangle(geometry, frame):
    covers_canvas = geometry.intersection(frame).area >= BACKGROUND_COVERAGE * frame.area
    is_rectangle = geometry.area >= BACKGROUND_RECTANGULARITY * box(*geometry.bounds).area
    return covers_canvas and is_rectangle


def _split_background(paint, viewbox):
    """Peel off bottom rectangles that blanket the canvas while other art sits on top."""
    if viewbox is None:
        return paint, None
    frame = box(viewbox[0], viewbox[1], viewbox[0] + viewbox[2], viewbox[1] + viewbox[3])
    background_fill = None
    while paint:
        bottom = paint[0]
        other_elements = [entry for entry in paint if entry.element != bottom.element]
        if not other_elements or not _is_canvas_rectangle(bottom.geometry, frame):
            break
        background_fill = bottom.fill
        paint = other_elements
    return paint, background_fill


def _visible_by_fill(paint):
    """Area each fill color still shows once later elements have painted over it."""
    geometries = [entry.geometry for entry in paint]
    tree = shapely.STRtree(geometries)
    visible = {}
    for index, entry in enumerate(paint):
        above = [geometries[i] for i in tree.query(entry.geometry) if i > index]
        shown = robust_difference(entry.geometry, robust_union(above)) if above else entry.geometry
        if not shown.is_empty:
            visible.setdefault(entry.fill, []).append(shown)
    return {fill: robust_union(parts) for fill, parts in visible.items()}


def polygon_parts(geometry):
    if geometry.geom_type == 'Polygon':
        return [] if geometry.is_empty else [geometry]
    return [part for g in getattr(geometry, 'geoms', []) for part in polygon_parts(g)]


def regions_from_fills(geometry_by_fill):
    """One Region per connected polygon, ordered by fill then position."""
    regions = []
    for fill, geometry in geometry_by_fill.items():
        pieces = [p for p in polygon_parts(geometry) if p.area > 0]
        for piece in sorted(pieces, key=lambda p: (p.bounds[1], p.bounds[0])):
            regions.append(Region(len(regions), fill, piece))
    return regions


def _load_root(svg_file):
    try:
        return ET.parse(svg_file).getroot()
    except UnsafeXmlError as error:
        raise ValueError(
            'This SVG declares XML entities (usually Illustrator editing data). '
            'Re-export it as a plain SVG.'
        ) from error


def parse_svg(svg_file, samples_per_segment=20, keep_background=False, void_fills=()):
    """
    Parse an SVG into colored regions.

    A plain rectangle that blankets the canvas beneath the artwork is treated
    as a background and dropped unless keep_background is set. Anything
    painted in the background color, or in one of void_fills, cuts a hole
    instead of becoming a region.
    """
    root = _load_root(svg_file)
    viewbox = _parse_viewbox(root)
    ctx = _WalkContext(samples=samples_per_segment, viewport=viewbox[2:] if viewbox else (0.0, 0.0))
    ctx.css_rules = _parse_stylesheet(root, ctx)
    _collect_paint(root, np.identity(3), dict(ROOT_STYLE), 1.0, ctx)

    paint, background_fill = ctx.paint, None
    if not keep_background:
        paint, background_fill = _split_background(paint, viewbox)

    voids = {normalize_color(fill) for fill in ([void_fills] if isinstance(void_fills, str) else void_fills)}
    if background_fill is not None:
        voids.add(background_fill)
        ctx.warn(f'Dropped a {background_fill} background rectangle. '
                 'Shapes in that color become cutouts. Keep the background to print it.')
    visible = {fill: g for fill, g in _visible_by_fill(paint).items() if fill not in voids}

    regions = regions_from_fills(visible)
    if not regions and paint:
        ctx.warn('All artwork matches a background or void color, so nothing is left to print.')
    elif not regions:
        ctx.warn('No filled shapes found in the SVG.')
    return ParsedSvg(regions, viewbox, background_fill, ctx.warnings)
