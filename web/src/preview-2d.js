// Pure Canvas2D renderers (no deps):
//   draw2dPreview  — read-only batch preview (polygon outline, skeleton,
//                    pyramid footprints / centers). One ring set per result
//                    card; mirrors the desktop app's debug viz.
//   drawEditor     — Phase 2 interactive editor view (per-component fills
//                    using each body's color, selection outline, holes
//                    cut out, packed positions per body). Returns the
//                    affine transform so click handlers can invert
//                    pixel coords back to SVG space for hit_test.

export function draw2dPreview(canvas, data) {
  const { rings, skeleton, positions, pyramidSize } = data;
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;

  // Crisp on HiDPI: backing store at dpr, CSS dimensions stay logical.
  const cssW = canvas.clientWidth;
  const cssH = canvas.clientHeight;
  canvas.width = cssW * dpr;
  canvas.height = cssH * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  ctx.clearRect(0, 0, cssW, cssH);

  if (!rings || rings.length === 0) return;

  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const ring of rings) {
    for (const [x, y] of ring) {
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
    }
  }
  const w = maxX - minX, h = maxY - minY;
  if (w <= 0 || h <= 0) return;

  const pad = 12;
  const scale = Math.min((cssW - 2 * pad) / w, (cssH - 2 * pad) / h);
  const ox = pad + (cssW - 2 * pad - w * scale) / 2;
  const oy = pad + (cssH - 2 * pad - h * scale) / 2;
  const tx = (x) => ox + (x - minX) * scale;
  const ty = (y) => oy + (y - minY) * scale;

  // Polygon outlines (one path per ring — handles MultiPolygon SVGs)
  ctx.strokeStyle = '#007aff';
  ctx.lineWidth = 1.5;
  for (const ring of rings) {
    ctx.beginPath();
    for (let i = 0; i < ring.length; i++) {
      const [x, y] = ring[i];
      if (i === 0) ctx.moveTo(tx(x), ty(y));
      else ctx.lineTo(tx(x), ty(y));
    }
    ctx.closePath();
    ctx.stroke();
  }

  // Skeleton points
  ctx.fillStyle = 'rgba(52, 199, 89, 0.55)';
  for (const [x, y] of skeleton) {
    ctx.fillRect(tx(x) - 0.5, ty(y) - 0.5, 1, 1);
  }

  // Pyramid footprints (rotated square diamond, mirrors create_pyramid_footprint)
  ctx.strokeStyle = 'rgba(255, 59, 48, 0.4)';
  ctx.lineWidth = 0.6;
  const cornerDist = (pyramidSize / 2) * Math.SQRT2;
  for (const pos of positions) {
    const [x, y, rot = 0] = pos;
    const baseAngle = (45 + rot) * Math.PI / 180;
    ctx.beginPath();
    for (let i = 0; i < 4; i++) {
      const a = baseAngle + i * Math.PI / 2;
      const cx = x + cornerDist * Math.cos(a);
      const cy = y + cornerDist * Math.sin(a);
      if (i === 0) ctx.moveTo(tx(cx), ty(cy));
      else ctx.lineTo(tx(cx), ty(cy));
    }
    ctx.closePath();
    ctx.stroke();
  }

  // Pyramid centers (red dots)
  ctx.fillStyle = '#ff3b30';
  for (const pos of positions) {
    const [x, y] = pos;
    ctx.beginPath();
    ctx.arc(tx(x), ty(y), 1.6, 0, Math.PI * 2);
    ctx.fill();
  }
}

const DEFAULT_BODY_COLOR = '#9ca3af';
const SELECTED_OUTLINE = '#007aff';
const SELECTED_OUTLINE_WIDTH = 3;
const COMPONENT_OUTLINE = '#1d1d1f';
const COMPONENT_OUTLINE_WIDTH = 0.8;

function svgFromState(state) {
  // Map component_id -> body for color lookup.
  const componentToBody = new Map();
  for (const body of state.bodies) {
    for (const cid of body.component_ids) componentToBody.set(cid, body);
  }
  const selected = new Set(state.selected_component_ids || []);
  return { componentToBody, selected };
}

/**
 * Render the Phase 2 editor view. Returns a transform descriptor:
 *   { tx, ty, fromPixel: (px, py) => [svgX, svgY] }
 * so the caller can convert canvas pixel clicks into SVG coords for
 * hit_test(). All drawing happens on the supplied canvas.
 */
export function drawEditor(canvas, state) {
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth;
  const cssH = canvas.clientHeight;
  canvas.width = cssW * dpr;
  canvas.height = cssH * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  const components = state.components || [];
  if (components.length === 0) return null;

  // Bounds across all exteriors.
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const c of components) {
    for (const [x, y] of c.exterior) {
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
    }
  }
  const w = maxX - minX, h = maxY - minY;
  if (w <= 0 || h <= 0) return null;

  const pad = 16;
  const scale = Math.min((cssW - 2 * pad) / w, (cssH - 2 * pad) / h);
  const ox = pad + (cssW - 2 * pad - w * scale) / 2;
  const oy = pad + (cssH - 2 * pad - h * scale) / 2;
  const tx = (x) => ox + (x - minX) * scale;
  const ty = (y) => oy + (y - minY) * scale;
  const fromPixel = (px, py) => [(px - ox) / scale + minX, (py - oy) / scale + minY];

  const { componentToBody, selected } = svgFromState(state);

  // Two-pass render: fills first, then selection outlines on top.
  for (const component of components) {
    const body = componentToBody.get(component.id);
    const fill = body?.color_hex || component.source_fill || DEFAULT_BODY_COLOR;
    const disabled = body && body.enabled === false;
    ctx.fillStyle = disabled ? 'rgba(170,170,170,0.35)' : fill;
    ctx.strokeStyle = COMPONENT_OUTLINE;
    ctx.lineWidth = COMPONENT_OUTLINE_WIDTH;
    ctx.beginPath();
    const ext = component.exterior;
    for (let i = 0; i < ext.length; i++) {
      const [x, y] = ext[i];
      if (i === 0) ctx.moveTo(tx(x), ty(y));
      else ctx.lineTo(tx(x), ty(y));
    }
    ctx.closePath();
    // Cut holes via subpath winding (Canvas2D uses non-zero by default;
    // even-odd respects the inner subpath as a hole).
    for (const hole of component.holes || []) {
      for (let i = 0; i < hole.length; i++) {
        const [x, y] = hole[i];
        if (i === 0) ctx.moveTo(tx(x), ty(y));
        else ctx.lineTo(tx(x), ty(y));
      }
      ctx.closePath();
    }
    ctx.fill('evenodd');
    ctx.stroke();
  }

  // Selection overlay outline (drawn after fills so it sits on top).
  ctx.strokeStyle = SELECTED_OUTLINE;
  ctx.lineWidth = SELECTED_OUTLINE_WIDTH;
  for (const component of components) {
    if (!selected.has(component.id)) continue;
    ctx.beginPath();
    const ext = component.exterior;
    for (let i = 0; i < ext.length; i++) {
      const [x, y] = ext[i];
      if (i === 0) ctx.moveTo(tx(x), ty(y));
      else ctx.lineTo(tx(x), ty(y));
    }
    ctx.closePath();
    ctx.stroke();
  }

  // Pyramid centers per body (small dots; size is uniform for legibility).
  if (state.positions_by_body) {
    for (const [bidStr, positions] of Object.entries(state.positions_by_body)) {
      const bid = Number(bidStr);
      const body = state.bodies.find((b) => b.id === bid);
      if (!body || body.enabled === false) continue;
      ctx.fillStyle = 'rgba(29,29,31,0.7)';
      for (const pos of positions) {
        const [x, y] = pos;
        ctx.beginPath();
        ctx.arc(tx(x), ty(y), 1.4, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  }

  return { tx, ty, fromPixel };
}
