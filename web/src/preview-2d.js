// Top-down drawing of a pad design, from PadDesign.preview(): the floor the
// pyramids stand on, the holes in it, centerline points and pyramid bases.
// Pure Canvas2D, no deps. Design coordinates are millimetres with Y up; the
// canvas has Y down, so every point goes through toCanvas().

const FLOOR_FILL = '#dbe9fb';
const FLOOR_OUTLINE = '#007aff';
const RECESS_FILL = '#aebdd1';
const HOLE_OUTLINE = '#8e8e93';
const SKELETON_FILL = 'rgba(52, 199, 89, 0.55)';
const PYRAMID_OUTLINE = 'rgba(255, 59, 48, 0.55)';
const PYRAMID_CENTER = '#ff3b30';
const MARGIN_PX = 12;

function tracePolygons(ctx, polygons, toCanvas) {
  ctx.beginPath();
  for (const rings of polygons) {
    for (const ring of rings) {
      ring.forEach(([x, y], index) => {
        const [px, py] = toCanvas(x, y);
        if (index === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      });
      ctx.closePath();
    }
  }
}

function drawPyramidBases(ctx, design, toCanvas) {
  const cornerRadius = design.pyramid_size / Math.SQRT2;
  const sides = design.pyramid_sides;
  ctx.strokeStyle = PYRAMID_OUTLINE;
  ctx.lineWidth = 0.8;
  for (const [x, y, rotation] of design.positions) {
    ctx.beginPath();
    for (let corner = 0; corner < sides; corner++) {
      const angle = ((45 + rotation + (corner * 360) / sides) * Math.PI) / 180;
      const [px, py] = toCanvas(x + cornerRadius * Math.cos(angle), y + cornerRadius * Math.sin(angle));
      if (corner === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
    }
    ctx.closePath();
    ctx.stroke();
  }

  ctx.fillStyle = PYRAMID_CENTER;
  for (const [x, y] of design.positions) {
    const [px, py] = toCanvas(x, y);
    ctx.beginPath();
    ctx.arc(px, py, 1.4, 0, Math.PI * 2);
    ctx.fill();
  }
}

export function draw2dPreview(canvas, design) {
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;

  // Crisp on HiDPI: backing store at dpr, CSS dimensions stay logical.
  const cssWidth = canvas.clientWidth;
  const cssHeight = canvas.clientHeight;
  canvas.width = cssWidth * dpr;
  canvas.height = cssHeight * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssWidth, cssHeight);

  if (!(design.width > 0) || !(design.height > 0)) return;

  // The art always starts at the origin, so width and height are its extent.
  const scale = Math.min(
    (cssWidth - 2 * MARGIN_PX) / design.width,
    (cssHeight - 2 * MARGIN_PX) / design.height,
  );
  const left = (cssWidth - design.width * scale) / 2;
  const top = (cssHeight - design.height * scale) / 2;
  const toCanvas = (x, y) => [left + x * scale, top + (design.height - y) * scale];

  // 'evenodd' leaves each polygon's hole rings unfilled.
  tracePolygons(ctx, design.art, toCanvas);
  ctx.fillStyle = FLOOR_FILL;
  ctx.fill('evenodd');
  ctx.strokeStyle = FLOOR_OUTLINE;
  ctx.lineWidth = 1.5;
  ctx.stroke();

  // A recessed hole still has a floor, so it is shaded. A through hole stays
  // empty and only gets a dashed edge.
  tracePolygons(ctx, design.voids, toCanvas);
  if (design.voids_recessed) {
    ctx.fillStyle = RECESS_FILL;
    ctx.fill('evenodd');
  }
  ctx.strokeStyle = HOLE_OUTLINE;
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 3]);
  ctx.stroke();
  ctx.setLineDash([]);

  ctx.fillStyle = SKELETON_FILL;
  for (const [x, y] of design.skeleton) {
    const [px, py] = toCanvas(x, y);
    ctx.fillRect(px - 0.5, py - 0.5, 1, 1);
  }

  drawPyramidBases(ctx, design, toCanvas);
}
