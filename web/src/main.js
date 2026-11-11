// Entry point. Bootstraps Pyodide, loads the stomppad package (synced from the
// parent project), and processes uploaded SVGs sequentially via the same
// Python pipeline the desktop app uses. STL rendering happens in-browser via
// openscad-wasm.

import { renderStl } from './scad-renderer.js';
import { draw2dPreview } from './preview-2d.js';
import { mount3dPreview } from './preview-3d.js';

const PYODIDE_VERSION = 'v0.26.4';
const PACKAGE_URL = '/stomppad';

// Form fields sent to PadSettings.from_params, which rejects names it doesn't know.
const NUMBER_FIELDS = [
  'target_width', 'target_height', 'samples_per_segment', 'skeleton_resolution',
  'pyramid_size', 'pyramid_spacing', 'safety_margin',
  'base_thickness', 'outline_offset', 'outline_height', 'pyramid_height', 'pyramid_style',
  'recess_depth',
];
const WHOLE_NUMBER_FIELDS = new Set(['samples_per_segment', 'pyramid_style']);
const CHECKBOX_FIELDS = ['include_rotation', 'keep_background'];

const consoleEl = document.getElementById('console');
const resultsEl = document.getElementById('results');
const fileListEl = document.getElementById('file-list');
const fileInput = document.getElementById('file-input');
const processBtn = document.getElementById('process-btn');
const stopBtn = document.getElementById('stop-btn');
const clearBtn = document.getElementById('clear-btn');
const paramsForm = document.getElementById('params-form');

let pyodide = null;
let pendingFiles = [];
let stopRequested = false;
const fileStatuses = new Map();  // filename -> 'pending' | 'working' | 'done' | 'error'

// three.js viewers leak WebGL contexts if their dispose() is never called.
// Browsers cap active contexts (~16 in Chrome) — track every mounted handle
// so we can release them on Clear and on each new Process All run.
const active3dHandles = new Set();

function disposeAll3d() {
  for (const handle of active3dHandles) {
    try { handle.dispose(); } catch { /* ignore */ }
  }
  active3dHandles.clear();
}

// A C++ exception inside the geometry library kills Pyodide outright. Python
// cannot catch it, and the runPythonAsync promise that was waiting never
// settles: the error only surfaces as an uncaught global error. This promise
// rejects when that happens, so callers can race against it.
const runtimeCrash = new Promise((_, reject) => {
  window.addEventListener('error', (event) => {
    if (event.error && event.error.pyodide_fatal_error) reject(event.error);
  });
  window.addEventListener('unhandledrejection', (event) => {
    if (event.reason && event.reason.pyodide_fatal_error) reject(event.reason);
  });
});
runtimeCrash.catch(() => {});

function log(msg) {
  const ts = new Date().toLocaleTimeString();
  consoleEl.textContent += `[${ts}] ${msg}\n`;
  consoleEl.scrollTop = consoleEl.scrollHeight;
}

function readSettings() {
  const data = new FormData(paramsForm);
  const settings = { void_mode: data.get('void_mode') };
  for (const name of NUMBER_FIELDS) {
    const text = data.get(name);
    const value = Number(text);
    if (text === null || text.trim() === '' || !Number.isFinite(value)) {
      throw new Error(`"${name.replaceAll('_', ' ')}" needs a number`);
    }
    settings[name] = WHOLE_NUMBER_FIELDS.has(name) ? Math.trunc(value) : value;
  }
  for (const name of CHECKBOX_FIELDS) {
    settings[name] = data.get(name) === 'on';
  }
  return settings;
}

// A fatal Pyodide error, a WebAssembly trap or running out of memory all leave
// the Python runtime unusable. An ordinary Python exception does not.
function runtimeIsDead(error) {
  return Boolean(error.pyodide_fatal_error)
    || error instanceof WebAssembly.RuntimeError
    || error instanceof RangeError;
}

// Pyodide wraps Python exceptions in a full traceback. The last line is the
// message the pipeline wrote for the user.
function describeError(error) {
  const message = String(error.message || error).trim();
  return error.type ? message.split('\n').pop() : message;
}

function refreshFileList() {
  fileListEl.innerHTML = '';
  for (const f of pendingFiles) {
    const status = fileStatuses.get(f.name) || 'pending';
    const li = document.createElement('li');
    li.dataset.status = status;
    const name = document.createElement('span');
    name.className = 'file-name';
    name.textContent = f.name;
    const meta = document.createElement('span');
    meta.className = 'file-meta';
    meta.textContent = `${(f.size / 1024).toFixed(1)} KB`;
    const statusEl = document.createElement('span');
    statusEl.className = 'file-status';
    statusEl.textContent = status;
    li.append(name, meta, statusEl);
    fileListEl.appendChild(li);
  }
}

function setFileStatus(filename, status) {
  fileStatuses.set(filename, status);
  refreshFileList();
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
  // Hold the blob URL long enough for slow connections / Save-As dialogs.
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

function addResult(file, scad, stl, preview, stlFailed) {
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
  meta.textContent = `${preview.width.toFixed(1)} × ${preview.height.toFixed(1)} mm, `
    + `${preview.positions.length} pyramids${stlFailed ? ', STL failed (see console)' : ''}`;
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
    mount3dPreview(viewer3dContainer, stl).then((handle) => {
      active3dHandles.add(handle);
    }).catch((e) =>
      log(`ERROR mounting 3D preview for ${file.name}: ${e.message || e}`)
    );
  }
}

async function initPyodide() {
  log('Loading Pyodide runtime…');
  pyodide = await window.loadPyodide({
    indexURL: `https://cdn.jsdelivr.net/pyodide/${PYODIDE_VERSION}/full/`,
  });
  log('Loading scientific Python packages (numpy, scipy, scikit-image, shapely, pyclipper)…');
  await pyodide.loadPackage(['numpy', 'scipy', 'scikit-image', 'shapely', 'pyclipper', 'micropip']);
  log('Installing svg.path and defusedxml from PyPI…');
  const micropip = pyodide.pyimport('micropip');
  await micropip.install(['svg.path', 'defusedxml']);
  log('Loading stomppad package…');
  await loadGeometryPackage();
  pyodide.runPython(`
import sys
if '/' not in sys.path:
    sys.path.insert(0, '/')
from stomppad.openscad import generate_openscad
from stomppad.pad import PadSettings, build_pad
`);
  log('Pyodide ready.');
}

// no-cache makes the browser revalidate, so a deploy can't leave it running
// a mix of old and new modules.
async function fetchOk(url) {
  const response = await fetch(url, { cache: 'no-cache' });
  if (!response.ok) throw new Error(`Failed to fetch ${url}: ${response.status}`);
  return response;
}

async function loadGeometryPackage() {
  const moduleNames = await (await fetchOk(`${PACKAGE_URL}/manifest.json`)).json();
  pyodide.FS.mkdirTree('/stomppad');
  await Promise.all(moduleNames.map(async (name) => {
    const source = await (await fetchOk(`${PACKAGE_URL}/${name}`)).text();
    pyodide.FS.writeFile(`/stomppad/${name}`, source);
  }));
}

async function processOne(file, settings) {
  log(`\n=== Processing ${file.name} ===`);
  const svgText = await file.text();
  pyodide.FS.writeFile('/input.svg', svgText);
  pyodide.globals.set('js_log', (msg) => log(`[${file.name}] ${msg}`));
  // toPy returns a PyProxy we own — hold the reference so we can destroy it
  // explicitly after the run instead of relying on Pyodide's GC.
  const settingsProxy = pyodide.toPy(settings);
  pyodide.globals.set('js_settings', settingsProxy);

  // One call blocks the page until the design is built (the real fix is a
  // Web Worker). Awaiting it at least lets the console repaint between files.
  await Promise.race([runtimeCrash, pyodide.runPythonAsync(`
design = build_pad('/input.svg', PadSettings.from_params(js_settings), logger=js_log)
scad_source = generate_openscad(design)
preview_data = design.preview()
`).finally(() => settingsProxy.destroy())]);

  const scad = pyodide.globals.get('scad_source');
  const previewProxy = pyodide.globals.get('preview_data');
  try {
    return { scad, preview: previewProxy.toJs({ dict_converter: Object.fromEntries }) };
  } finally {
    previewProxy.destroy();
  }
}

async function renderStlForFile(file, scad) {
  log(`[${file.name}] Rendering STL via openscad-wasm (first run loads ~8 MB WASM)…`);
  const t0 = performance.now();
  const stl = await renderStl(scad);
  const ms = (performance.now() - t0).toFixed(0);
  log(`[${file.name}] STL rendered (${(stl.length / 1024).toFixed(1)} KB in ${ms} ms)`);
  return stl;
}

fileInput.addEventListener('change', () => {
  pendingFiles = Array.from(fileInput.files);
  fileStatuses.clear();
  refreshFileList();
});

stopBtn.addEventListener('click', () => {
  stopRequested = true;
  stopBtn.disabled = true;
  log('Stop requested — finishing current file then aborting.');
});

clearBtn.addEventListener('click', () => {
  disposeAll3d();
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
  stopRequested = false;
  processBtn.disabled = true;
  processBtn.textContent = 'Processing…';
  stopBtn.disabled = false;
  for (const f of pendingFiles) setFileStatus(f.name, 'pending');
  const generateStl = new FormData(paramsForm).get('generate_stl') === 'on';
  let settings = null;
  try {
    settings = readSettings();
  } catch (e) {
    log(`ERROR: ${e.message}`);
  }
  for (const file of settings ? pendingFiles : []) {
    if (stopRequested) {
      log('Stopped.');
      break;
    }
    setFileStatus(file.name, 'working');
    let design;
    try {
      design = await processOne(file, settings);
    } catch (e) {
      setFileStatus(file.name, 'error');
      if (runtimeIsDead(e)) {
        // Nothing more can run until the page is reloaded.
        pyodide = null;
        log(`ERROR: the geometry engine crashed on ${file.name} and took the Python runtime `
          + 'with it. Reload the page to process other files.');
        break;
      }
      log(`ERROR processing ${file.name}: ${describeError(e)}`);
      continue;
    }

    let stl = null;
    let stlFailed = false;
    if (generateStl) {
      try {
        stl = await renderStlForFile(file, design.scad);
      } catch (e) {
        stlFailed = true;
        log(`ERROR rendering STL for ${file.name}: ${e.message || e}`);
      }
    }
    addResult(file, design.scad, stl, design.preview, stlFailed);
    setFileStatus(file.name, 'done');
  }
  processBtn.disabled = pyodide === null;
  processBtn.textContent = pyodide === null ? 'Reload to continue' : 'Process all';
  stopBtn.disabled = true;
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
