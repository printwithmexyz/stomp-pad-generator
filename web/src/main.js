// Entry point. Bootstraps Pyodide, loads the calculator (synced from parent),
// and processes uploaded SVGs sequentially via the same Python pipeline the
// desktop app uses. STL rendering happens in-browser via openscad-wasm.

import { renderStl } from './scad-renderer.js';
import { draw2dPreview } from './preview-2d.js';
import { mount3dPreview } from './preview-3d.js';

const PYODIDE_VERSION = 'v0.26.4';

const consoleEl = document.getElementById('console');
const resultsEl = document.getElementById('results');
const fileListEl = document.getElementById('file-list');
const fileInput = document.getElementById('file-input');
const processBtn = document.getElementById('process-btn');
const clearBtn = document.getElementById('clear-btn');
const paramsForm = document.getElementById('params-form');

let pyodide = null;
let pendingFiles = [];

function log(msg) {
  const ts = new Date().toLocaleTimeString();
  consoleEl.textContent += `[${ts}] ${msg}\n`;
  consoleEl.scrollTop = consoleEl.scrollHeight;
}

function readParams() {
  const data = new FormData(paramsForm);
  return {
    target_width: parseFloat(data.get('target_width')),
    target_height: parseFloat(data.get('target_height')),
    samples_per_segment: parseInt(data.get('samples_per_segment'), 10),
    skeleton_resolution: parseFloat(data.get('skeleton_resolution')),
    pyramid_size: parseFloat(data.get('pyramid_size')),
    pyramid_spacing: parseFloat(data.get('pyramid_spacing')),
    safety_margin: parseFloat(data.get('safety_margin')),
    include_rotation: data.get('include_rotation') === 'on',
    base_thickness: parseFloat(data.get('base_thickness')),
    outline_offset: parseFloat(data.get('outline_offset')),
    outline_height: parseFloat(data.get('outline_height')),
    pyramid_height: parseFloat(data.get('pyramid_height')),
    pyramid_style: parseInt(data.get('pyramid_style'), 10),
    generate_stl: data.get('generate_stl') === 'on',
  };
}

function refreshFileList() {
  fileListEl.innerHTML = '';
  for (const f of pendingFiles) {
    const li = document.createElement('li');
    li.textContent = `${f.name} (${(f.size / 1024).toFixed(1)} KB)`;
    fileListEl.appendChild(li);
  }
}

function download(data, filename, mime) {
  const blob = data instanceof Blob ? data : new Blob([data], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function addResult(file, scad, svg, stl, preview) {
  const stem = file.name.replace(/\.svg$/i, '');
  const card = document.createElement('article');
  card.className = 'result';

  const header = document.createElement('header');
  header.className = 'result-header';
  const name = document.createElement('span');
  name.className = 'result-name';
  name.textContent = file.name;
  const meta = document.createElement('span');
  meta.className = 'result-meta';
  meta.textContent = `${preview.positions.length} pyramids`;
  header.append(name, meta);
  card.appendChild(header);

  const previewRow = document.createElement('div');
  previewRow.className = 'result-previews';

  const canvas2d = document.createElement('canvas');
  canvas2d.className = 'preview-2d';
  previewRow.appendChild(canvas2d);

  let viewer3dContainer = null;
  if (stl) {
    viewer3dContainer = document.createElement('div');
    viewer3dContainer.className = 'preview-3d';
    previewRow.appendChild(viewer3dContainer);
  }

  card.appendChild(previewRow);

  const footer = document.createElement('footer');
  footer.className = 'result-actions';
  const scadBtn = document.createElement('button');
  scadBtn.textContent = `${stem}.scad`;
  scadBtn.addEventListener('click', () =>
    download(scad, `${stem}.scad`, 'application/x-openscad')
  );
  footer.appendChild(scadBtn);

  const svgBtn = document.createElement('button');
  svgBtn.textContent = file.name;
  svgBtn.addEventListener('click', () =>
    download(svg, file.name, 'image/svg+xml')
  );
  footer.appendChild(svgBtn);

  if (stl) {
    const stlBtn = document.createElement('button');
    stlBtn.textContent = `${stem}.stl`;
    stlBtn.addEventListener('click', () =>
      download(stl, `${stem}.stl`, 'model/stl')
    );
    footer.appendChild(stlBtn);
  }

  card.appendChild(footer);
  resultsEl.appendChild(card);

  // Draw after attach so clientWidth/Height are non-zero.
  requestAnimationFrame(() => draw2dPreview(canvas2d, preview));
  if (viewer3dContainer && stl) {
    mount3dPreview(viewer3dContainer, stl).catch((e) =>
      log(`ERROR mounting 3D preview for ${file.name}: ${e.message || e}`)
    );
  }
}

async function initPyodide() {
  log('Loading Pyodide runtime…');
  pyodide = await window.loadPyodide({
    indexURL: `https://cdn.jsdelivr.net/pyodide/${PYODIDE_VERSION}/full/`,
  });
  log('Loading scientific Python packages (numpy, scipy, scikit-image, shapely)…');
  await pyodide.loadPackage(['numpy', 'scipy', 'scikit-image', 'shapely', 'micropip']);
  log('Installing svg.path from PyPI…');
  const micropip = pyodide.pyimport('micropip');
  await micropip.install('svg.path');
  log('Loading calculator module…');
  const resp = await fetch('/pyramid_position_calculator.py');
  if (!resp.ok) throw new Error(`Failed to fetch calculator: ${resp.status}`);
  const src = await resp.text();
  pyodide.FS.writeFile('/pyramid_position_calculator.py', src);
  pyodide.runPython(`
import sys
if '/' not in sys.path:
    sys.path.insert(0, '/')
import pyramid_position_calculator
`);
  log('Pyodide ready.');
}

async function processOne(file, params) {
  log(`\n=== Processing ${file.name} ===`);
  const svgText = await file.text();
  pyodide.FS.writeFile('/input.svg', svgText);
  pyodide.globals.set('js_log', (msg) => log(`[${file.name}] ${msg}`));
  pyodide.globals.set('js_params', pyodide.toPy(params));
  pyodide.globals.set('js_filename', file.name);
  pyodide.runPython(`
from pyramid_position_calculator import (
    parse_svg_to_polygon,
    calculate_skeleton,
    calculate_valid_pyramid_positions,
    generate_openscad_with_positions,
)

def py_logger(msg):
    js_log(msg)

p = js_params
polygon, svg_info = parse_svg_to_polygon(
    '/input.svg',
    target_width=p['target_width'],
    target_height=p['target_height'] if p['target_height'] > 0 else None,
    samples_per_segment=p['samples_per_segment'],
    logger=py_logger,
)
skeleton_points = calculate_skeleton(polygon, resolution=p['skeleton_resolution'])
valid_positions = calculate_valid_pyramid_positions(
    polygon,
    pyramid_size=p['pyramid_size'],
    pyramid_spacing=p['pyramid_spacing'],
    target_width=p['target_width'],
    include_rotation=p['include_rotation'],
    safety_margin=p['safety_margin'],
    logger=py_logger,
)
generate_openscad_with_positions(
    js_filename,
    valid_positions,
    '/output.scad',
    svg_info=svg_info,
    logger=py_logger,
    target_width=p['target_width'],
    base_thickness=p['base_thickness'],
    outline_offset=p['outline_offset'],
    outline_height=p['outline_height'],
    pyramid_size=p['pyramid_size'],
    pyramid_height=p['pyramid_height'],
    pyramid_style=p['pyramid_style'],
)

def _polygon_rings(p):
    if p.geom_type == 'Polygon':
        return [list(p.exterior.coords)]
    if p.geom_type == 'MultiPolygon':
        return [list(g.exterior.coords) for g in p.geoms]
    return []

preview_data = {
    'rings': [
        [[float(x), float(y)] for x, y in ring]
        for ring in _polygon_rings(polygon)
    ],
    'skeleton': [[float(x), float(y)] for x, y in skeleton_points],
    'positions': [
        [float(pos[0]), float(pos[1]), float(pos[2]) if len(pos) > 2 else 0.0]
        for pos in valid_positions
    ],
}
`);
  const scad = pyodide.FS.readFile('/output.scad', { encoding: 'utf8' });
  const preview = pyodide.globals.get('preview_data').toJs({ dict_converter: Object.fromEntries });
  preview.pyramidSize = params.pyramid_size;
  return { scad, svg: svgText, preview };
}

async function renderStlForFile(file, scad, svgText) {
  log(`[${file.name}] Rendering STL via openscad-wasm (first run loads ~8 MB WASM)…`);
  const t0 = performance.now();
  const stl = await renderStl(scad, file.name, svgText);
  const ms = (performance.now() - t0).toFixed(0);
  log(`[${file.name}] STL rendered (${(stl.length / 1024).toFixed(1)} KB in ${ms} ms)`);
  return stl;
}

fileInput.addEventListener('change', () => {
  pendingFiles = Array.from(fileInput.files);
  refreshFileList();
});

clearBtn.addEventListener('click', () => {
  resultsEl.innerHTML = '';
  consoleEl.textContent = '';
});

processBtn.addEventListener('click', async () => {
  if (!pyodide) {
    log('Pyodide not ready yet.');
    return;
  }
  if (pendingFiles.length === 0) {
    log('No SVG files selected.');
    return;
  }
  processBtn.disabled = true;
  processBtn.textContent = 'Processing…';
  const params = readParams();
  for (const file of pendingFiles) {
    try {
      const { scad, svg, preview } = await processOne(file, params);
      let stl = null;
      if (params.generate_stl) {
        try {
          stl = await renderStlForFile(file, scad, svg);
        } catch (e) {
          log(`ERROR rendering STL for ${file.name}: ${e.message || e}`);
        }
      }
      addResult(file, scad, svg, stl, preview);
    } catch (e) {
      log(`ERROR processing ${file.name}: ${e.message || e}`);
    }
  }
  processBtn.disabled = false;
  processBtn.textContent = 'Process all';
  log('\nDone.');
});

(async () => {
  try {
    await initPyodide();
    processBtn.disabled = false;
    processBtn.textContent = 'Process all';
  } catch (e) {
    log(`FATAL: ${e.message || e}`);
    processBtn.textContent = 'Failed to load';
  }
})();
