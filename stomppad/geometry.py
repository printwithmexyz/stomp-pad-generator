"""Component-aware SVG parsing.

The Phase 0 monolith built a single ``Polygon(all_points)`` from every path
in the SVG concatenated together. That collapsed multi-path files (the
smiley fixture from ``docs/v2-plan.md``) to whichever ring had the most
points — usually the background rectangle. v2 Phase 1.1 replaces that with
ring-aware parsing: each subpath/element becomes its own ring, even-odd
nesting decides which rings are holes vs. nested bodies, and a heuristic
drops near-full-canvas rectangular backgrounds before nesting is computed.

The legacy ``parse_svg_to_polygon`` in ``stomppad/__init__.py`` is now a
thin shim over ``parse_svg_to_components`` so single-path SVGs (the
regression fixture) keep producing byte-identical output.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from shapely.geometry import Polygon
from svg.path import parse_path

try:
    import defusedxml.ElementTree as ET
except ImportError:  # pragma: no cover — defusedxml is in requirements
    import xml.etree.ElementTree as ET


SVG_NS = "{http://www.w3.org/2000/svg}"

# Background auto-drop thresholds. A ring is auto-dropped when it covers
# >= COVERAGE * viewBox AND its bounding box is essentially full (a real
# rectangle's area == bbox area exactly; a blob's is strictly less). Both
# guards together are what v2-plan §1.1 requires — coverage alone would
# drop legitimate full-bleed badges.
DEFAULT_BACKGROUND_COVERAGE = 0.95
DEFAULT_BACKGROUND_BBOX_FILL = 0.999


def _log(logger, message):
    (logger or print)(message)


def _explain_xml_parse_error(svg_file, exc) -> str:
    """Render a multi-line diagnostic for an XML ParseError: the source
    line where the parser tripped, with a caret marking the column.

    Adobe Illustrator and similar tools regularly emit SVGs whose first
    line is a single 200+ char run (XML declaration + DOCTYPE +
    Generator comment + root element). When the parser raises
    ``not well-formed (invalid token): line 1, column 54``, the user has
    no easy way to see what's actually at column 54. This dumps the
    relevant slice so they can read it.

    Common culprits to look for in the dumped slice:
      - ``--`` inside an XML comment (forbidden by spec; Adobe ships
        these in older versions)
      - HTML entities like ``&middot;`` or ``&nbsp;`` that aren't
        defined in XML
      - Unescaped ``&`` in attribute values
      - Stray non-XML bytes (BOM mid-file, control characters)
    """
    line_no, col_no = getattr(exc, "position", (None, None))
    try:
        with open(svg_file, "rb") as f:
            raw = f.read()
    except OSError as read_exc:
        return f"  (could not re-read {svg_file!r} for diagnostic: {read_exc})"
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not lines or not isinstance(line_no, int) or line_no < 1 or line_no > len(lines):
        # Fall back to the file head.
        head = text[:200].replace("\n", "\\n")
        return f"  file head (first 200 chars): {head!r}"
    bad_line = lines[line_no - 1]
    # Window the offending column so even a single-line giant file is
    # readable: ±40 chars around the reported position.
    if isinstance(col_no, int) and col_no >= 0:
        start = max(0, col_no - 40)
        end = min(len(bad_line), col_no + 40)
        slice_ = bad_line[start:end]
        caret_offset = col_no - start
        caret = " " * caret_offset + "^"
        return (
            f"  near line {line_no}, col {col_no} (showing chars {start}..{end}):\n"
            f"    {slice_}\n"
            f"    {caret}"
        )
    return f"  line {line_no}: {bad_line[:200]!r}"


@dataclass
class Component:
    """One SVG component: an exterior ring plus optional interior holes.

    Coordinates are in scaled mm (post-resize); each ring is a closed list
    of ``(x, y)`` tuples (first == last). ``source_fill`` carries the SVG
    ``fill`` attribute (lowercased hex like ``#ffffff``) so Phase 2's
    editor can default a body's color from the imported file.
    """

    id: int
    exterior: list[tuple[float, float]]
    holes: list[list[tuple[float, float]]] = field(default_factory=list)
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    area: float = 0.0
    source_fill: Optional[str] = None

    def to_polygon(self) -> Polygon:
        return Polygon(self.exterior, holes=self.holes)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "exterior": [list(p) for p in self.exterior],
            "holes": [[list(p) for p in h] for h in self.holes],
            "bbox": list(self.bbox),
            "area": self.area,
            "source_fill": self.source_fill,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Component":
        return cls(
            id=d["id"],
            exterior=[tuple(p) for p in d["exterior"]],
            holes=[[tuple(p) for p in h] for h in d.get("holes", [])],
            bbox=tuple(d.get("bbox", (0.0, 0.0, 0.0, 0.0))),
            area=d.get("area", 0.0),
            source_fill=d.get("source_fill"),
        )


def _normalize_fill(fill: Optional[str]) -> Optional[str]:
    if not fill:
        return None
    f = fill.strip().lower()
    if f in ("none", "transparent"):
        return None
    return f


def _close_ring(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Append the first point if the ring isn't already closed (matches the
    legacy 0.1-unit tolerance the monolith used)."""
    if len(pts) < 3:
        return pts
    dx = pts[0][0] - pts[-1][0]
    dy = pts[0][1] - pts[-1][1]
    if dx * dx + dy * dy > 0.01:
        return pts + [pts[0]]
    return pts


def _sample_path_d(d: str, samples_per_segment: int) -> list[list[tuple[float, float]]]:
    """Split an SVG path ``d`` into one closed ring per subpath.

    The Phase 0 monolith concatenated every segment of every subpath into
    one big point list, so a compound path like ``M ... Z M ... Z`` rendered
    as a single weird polygon. Splitting on each ``Move`` is the
    SVG-correct behavior — and what later steps (nesting, holes) rely on.
    """
    path = parse_path(d)
    rings: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []

    def _finish():
        nonlocal current
        if len(current) >= 3:
            rings.append(_close_ring(current))
        current = []

    for seg in path:
        seg_type = type(seg).__name__
        if seg_type == "Move":
            _finish()
            current = [(seg.end.real, seg.end.imag)]
            continue
        if seg_type == "Close":
            # The closing line itself doesn't add geometry — _close_ring
            # appends the start if needed.
            _finish()
            continue
        for i in range(samples_per_segment):
            t = i / samples_per_segment
            point = seg.point(t)
            current.append((point.real, point.imag))
        current.append((seg.end.real, seg.end.imag))

    _finish()
    return rings


def _extract_rings(root, samples_per_segment: int) -> list[tuple[list[tuple[float, float]], Optional[str]]]:
    """Walk the SVG and produce ``(ring, fill)`` tuples in document order.

    Each path subpath, rect, polygon, circle, ellipse becomes one ring.
    ``fill`` is the element's ``fill`` attribute (normalized to lowercase
    hex), or ``None`` if absent / ``"none"``.
    """
    out: list[tuple[list[tuple[float, float]], Optional[str]]] = []

    for elem in root.findall(f".//{SVG_NS}path"):
        d = elem.get("d")
        if not d:
            continue
        fill = _normalize_fill(elem.get("fill"))
        for ring in _sample_path_d(d, samples_per_segment):
            out.append((ring, fill))

    for elem in root.findall(f".//{SVG_NS}polygon"):
        pts_str = elem.get("points", "").replace(",", " ").split()
        coords = [float(v) for v in pts_str]
        pts = [(coords[i], coords[i + 1]) for i in range(0, len(coords) - 1, 2)]
        if len(pts) >= 3:
            out.append((_close_ring(pts), _normalize_fill(elem.get("fill"))))

    for elem in root.findall(f".//{SVG_NS}rect"):
        x = float(elem.get("x", 0))
        y = float(elem.get("y", 0))
        w = float(elem.get("width", 0))
        h = float(elem.get("height", 0))
        if w > 0 and h > 0:
            ring = [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]
            out.append((ring, _normalize_fill(elem.get("fill"))))

    circle_samples = max(samples_per_segment * 2, 64)

    for elem in root.findall(f".//{SVG_NS}circle"):
        cx = float(elem.get("cx", 0))
        cy = float(elem.get("cy", 0))
        r = float(elem.get("r", 0))
        if r > 0:
            angles = np.linspace(0, 2 * np.pi, circle_samples, endpoint=True)
            ring = list(zip(
                (cx + r * np.cos(angles)).tolist(),
                (cy + r * np.sin(angles)).tolist(),
            ))
            out.append((ring, _normalize_fill(elem.get("fill"))))

    for elem in root.findall(f".//{SVG_NS}ellipse"):
        cx = float(elem.get("cx", 0))
        cy = float(elem.get("cy", 0))
        rx = float(elem.get("rx", 0))
        ry = float(elem.get("ry", 0))
        if rx > 0 and ry > 0:
            angles = np.linspace(0, 2 * np.pi, circle_samples, endpoint=True)
            ring = list(zip(
                (cx + rx * np.cos(angles)).tolist(),
                (cy + ry * np.sin(angles)).tolist(),
            ))
            out.append((ring, _normalize_fill(elem.get("fill"))))

    return out


def _is_background(p: Polygon, viewbox_area: float,
                   coverage_threshold: float, bbox_fill_threshold: float) -> bool:
    if viewbox_area <= 0:
        return False
    minx, miny, maxx, maxy = p.bounds
    bbox_area = (maxx - minx) * (maxy - miny)
    if bbox_area <= 0:
        return False
    coverage = p.area / viewbox_area
    bbox_fill = p.area / bbox_area
    return coverage >= coverage_threshold and bbox_fill >= bbox_fill_threshold


def _direct_parent(idx: int, polys: list[Polygon]) -> Optional[int]:
    """Return the index of the smallest polygon strictly containing
    ``polys[idx]``, or ``None`` if it's at depth 0.

    Uses the rep-point of ``polys[idx]`` to test against larger polygons.

    **Known limitation:** ``representative_point()`` is guaranteed inside
    the polygon, but for highly concave shapes (crescents, C-letters,
    notched paths) it can land close to a concave boundary — far enough
    that a sibling polygon's edge passes near the rep-point and
    ``other.contains(pt)`` returns false even though the concave polygon
    is geometrically inside ``other``. Phase 1's fixture set is all
    axis-aligned rectangles + circles where this can't trigger; Phase 2
    should revisit when real-world concave SVG paths appear.

    The area filter (``other.area > my_area``) is the other load-bearing
    guard: without it, a big polygon whose centroid happens to fall
    inside a smaller child (e.g. a concentric layout — the v2-plan smiley
    case) is wrongly tagged as the child's child. Equal-area polygons
    can't properly nest, so ``<=`` is the right comparison.
    """
    pt = polys[idx].representative_point()
    my_area = polys[idx].area
    smallest = None
    smallest_area = None
    for j, other in enumerate(polys):
        if j == idx or other.area <= my_area:
            continue
        if other.contains(pt):
            if smallest_area is None or other.area < smallest_area:
                smallest = j
                smallest_area = other.area
    return smallest


def _depth_of(idx: int, parents: list[Optional[int]]) -> int:
    """Walk the parent chain to compute even-odd nesting depth."""
    d = 0
    cur = parents[idx]
    while cur is not None:
        d += 1
        cur = parents[cur]
    return d


def parse_svg_to_components(
    svg_file,
    target_width: float = 100,
    target_height: Optional[float] = None,
    samples_per_segment: int = 20,
    background_coverage: float = DEFAULT_BACKGROUND_COVERAGE,
    background_bbox_fill: float = DEFAULT_BACKGROUND_BBOX_FILL,
    logger=None,
) -> Optional[tuple[list[Component], dict]]:
    """Parse ``svg_file`` into a list of :class:`Component` plus ``svg_info``.

    Each top-level ring becomes a body (even depth in the nesting graph);
    its direct odd-depth children attach as holes. Backgrounds that cover
    the full viewBox AND are axis-aligned rectangles are dropped (with a
    warning logged so batch users notice). The dropped rings stay around
    only as a log line — Phase 2's editor will be able to re-add them.

    Returns ``None`` if the SVG had no parseable rings (mirrors the legacy
    error path so the desktop GUI's existing checks keep working) or if
    the file is not well-formed XML — in the latter case the logger gets
    the parse-error location plus a short hex+text preview of the bytes
    near the offending column so the caller can diagnose what's wrong.
    """
    try:
        tree = ET.parse(svg_file)
    except ET.ParseError as exc:
        _log(logger, f"ERROR: SVG is not well-formed XML: {exc}")
        _log(logger, _explain_xml_parse_error(svg_file, exc))
        return None
    root = tree.getroot()

    viewBox = root.get("viewBox")
    svg_width = None
    svg_height = None
    viewbox_area = 0.0
    if viewBox:
        vb = [float(x) for x in viewBox.split()]
        svg_width = vb[2]
        svg_height = vb[3]
        viewbox_area = svg_width * svg_height

    raw = _extract_rings(root, samples_per_segment)

    if not raw:
        _log(logger, f"ERROR: No parseable shapes in {svg_file}")
        return None

    # Build polygons in viewBox coordinates so background detection and
    # nesting both work on the same untransformed space.
    polys_with_fill: list[tuple[Polygon, Optional[str]]] = []
    for ring, fill in raw:
        try:
            p = Polygon(ring)
        except (ValueError, TypeError):
            continue
        if not p.is_valid:
            p = p.buffer(0)
            if not p.is_valid or p.is_empty:
                continue
        if p.geom_type != "Polygon" or p.area <= 0:
            continue
        polys_with_fill.append((p, fill))

    if not polys_with_fill:
        _log(logger, f"ERROR: Could not build any valid polygons from {svg_file}")
        return None

    # Background drop. Pre-compute the candidate mask so the "keep >=1
    # component" guard sees the full picture up front — the earlier
    # in-loop counter mis-handled SVGs with multiple overlapping
    # near-full-canvas rects (drop count lagged the read, so the third
    # background in a 3-bg file would survive while the first two were
    # dropped).
    is_bg = [
        _is_background(p, viewbox_area, background_coverage, background_bbox_fill)
        for p, _ in polys_with_fill
    ]
    num_bg = sum(is_bg)
    will_drop = num_bg > 0 and (len(polys_with_fill) - num_bg) >= 1

    survivors: list[tuple[Polygon, Optional[str]]] = []
    dropped = 0
    for (p, fill), bg in zip(polys_with_fill, is_bg):
        if will_drop and bg:
            _log(
                logger,
                f"  WARNING: dropped background ring ({p.area:.0f} sq units, "
                f"{p.area / viewbox_area * 100:.1f}% of viewBox, fill={fill})",
            )
            dropped += 1
            continue
        survivors.append((p, fill))

    polys = [p for p, _ in survivors]
    fills = [f for _, f in survivors]

    # Nesting → parent index per ring → depth.
    parents = [_direct_parent(i, polys) for i in range(len(polys))]
    depths = [_depth_of(i, parents) for i in range(len(polys))]

    # Collect bodies (even depth). For each body, holes = direct odd-depth
    # children. Depth-2+ nested bodies appear as their own top-level
    # components (the v2 default: holes are empty; nested shapes are new
    # bodies).
    bodies_idx = [i for i, d in enumerate(depths) if d % 2 == 0]
    holes_for: dict[int, list[int]] = {i: [] for i in bodies_idx}
    for j, d in enumerate(depths):
        if d % 2 == 1 and parents[j] is not None:
            parent = parents[j]
            if parent in holes_for:
                holes_for[parent].append(j)

    # Combined post-drop bounds: aggregate of per-polygon bboxes. The v2
    # plan called for unary_union here, but for the only purpose we need
    # (the bounding rectangle that drives ``scale_factor``) the cheap
    # min/max over individual bounds is equivalent and avoids a slow GEOS
    # union over potentially many polygons.
    cminx = min(p.bounds[0] for p in polys)
    cminy = min(p.bounds[1] for p in polys)
    cmaxx = max(p.bounds[2] for p in polys)
    cmaxy = max(p.bounds[3] for p in polys)
    original_width = cmaxx - cminx
    original_height = cmaxy - cminy
    if original_width <= 0 or original_height <= 0:
        _log(logger, "ERROR: zero-width/height geometry after background drop")
        return None

    scale_factor_width = target_width / original_width
    if target_height is not None and target_height > 0:
        scale_factor_height = target_height / original_height
        scale_factor = min(scale_factor_width, scale_factor_height)
        scaling_mode = "fit-to-bounds"
        limiting_dim = "width" if scale_factor == scale_factor_width else "height"
    else:
        scale_factor = scale_factor_width
        scaling_mode = "fit-to-width"
        limiting_dim = "width"

    components: list[Component] = []
    for new_id, body_i in enumerate(bodies_idx):
        ext_raw = list(polys[body_i].exterior.coords)
        holes_raw = [list(polys[h].exterior.coords) for h in holes_for[body_i]]
        ext = [(x * scale_factor, y * scale_factor) for x, y in ext_raw]
        holes = [
            [(x * scale_factor, y * scale_factor) for x, y in h]
            for h in holes_raw
        ]
        scaled = Polygon(ext, holes=holes)
        components.append(
            Component(
                id=new_id,
                exterior=ext,
                holes=holes,
                bbox=scaled.bounds,
                area=scaled.area,
                source_fill=fills[body_i],
            )
        )

    final_minx = min(c.bbox[0] for c in components)
    final_miny = min(c.bbox[1] for c in components)
    final_maxx = max(c.bbox[2] for c in components)
    final_maxy = max(c.bbox[3] for c in components)
    final_width = final_maxx - final_minx
    final_height = final_maxy - final_miny

    _log(logger, f"  SVG parsed: {len(components)} component(s)"
                 + (f", {dropped} background dropped" if dropped else ""))
    _log(logger, f"  Original size: {original_width:.1f} x {original_height:.1f}")
    _log(logger, f"  Scaling mode: {scaling_mode} (limited by {limiting_dim})")
    _log(logger, f"  Scale factor: {scale_factor:.4f}")
    _log(logger, f"  Final size: {final_width:.1f} x {final_height:.1f}")

    svg_info = {
        "viewbox_width": svg_width * scale_factor if svg_width else final_width,
        "viewbox_height": svg_height * scale_factor if svg_height else final_height,
        "scale_factor": scale_factor,
        "final_width": final_width,
        "final_height": final_height,
        "background_dropped": dropped,
    }

    return components, svg_info
