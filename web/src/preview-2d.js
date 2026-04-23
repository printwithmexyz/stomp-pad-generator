// Mirrors the desktop app's save_debug_visualization: polygon outline,
// skeleton points, pyramid centers + footprints. Pure Canvas2D — no deps.

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
