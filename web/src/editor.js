// Single-file interactive editor (Phase 2.2). Holds the Pyodide-side
// ShapeProject + BodyOutputCache, owns the right sidebar DOM, dispatches
// click hits to Python's hit_test, and re-renders the canvas on every
// mutation.
//
// All geometry / packing / mutation logic lives in Python (stomppad.project
// + stomppad.cache). The JS side is a thin renderer + event router — this
// is the locked design from the Phase 2 interview ("no UI imports" on the
// Python side, stateful selection in the model).

import { drawEditor } from './preview-2d.js';
import { renderStl } from './scad-renderer.js';
import { buildZip } from './zip.js';

export function createEditor({ pyodide, root, log }) {
  // root: { canvas, sidebar, fileInput, downloadBtn, autosaveToggle, savedStamp }
  let editorHandle = null; // { dispose, state, refresh }

  async function loadSvgFile(file) {
    if (editorHandle) editorHandle.dispose();
    editorHandle = null;
    if (!file) return;

    log(`[editor] loading ${file.name}`);
    const svgText = await file.text();
    pyodide.FS.writeFile('/editor_input.svg', svgText);
    pyodide.globals.set('_editor_filename', file.name);

    // One-shot Python bootstrap: parse SVG, build project + cache, expose
    // both as Python globals the JS side will hit on every mutation.
    await pyodide.runPythonAsync(`
from stomppad import (
    parse_svg_to_components,
    ShapeProject,
    BodyOutputCache,
    generate_per_body_scads,
    patterns as _patterns,
)

_editor_parsed = parse_svg_to_components('/editor_input.svg', target_width=100)
if _editor_parsed is None:
    _editor_project = None
    _editor_cache = None
else:
    _editor_components, _editor_svg_info = _editor_parsed
    _editor_project = ShapeProject.default_from_components(
        _editor_components, _editor_svg_info,
    )
    _editor_cache = BodyOutputCache(_editor_project)

def _editor_state():
    if _editor_project is None:
        return None
    return _editor_project.to_editor_dict(
        positions_by_body=_editor_cache.all_positions(),
    )

def _editor_pattern_names():
    return list(_patterns.names())
`);

    // Pyodide converts Python's None to JS undefined for `globals.get`;
    // for live Python objects it returns a PyProxy.
    const projectHandle = pyodide.globals.get('_editor_project');
    if (projectHandle === undefined || projectHandle === null) {
      log(`[editor] failed to parse ${file.name}`);
      renderEmpty(`Could not parse ${file.name}.`);
      return;
    }
    projectHandle.destroy?.();
    editorHandle = mount(file);
    editorHandle.refresh();
    log(`[editor] ${file.name} ready`);
  }

  function mount(file) {
    const handlers = { click: null, save: null, toggle: null };
    let state = null;
    let transform = null;
    let autosaveTimer = null;

    function patternNames() {
      const fn = pyodide.globals.get('_editor_pattern_names');
      const proxy = fn();
      fn.destroy?.();
      const arr = proxy.toJs();
      proxy.destroy?.();
      return arr;
    }

    function pullState() {
      const fn = pyodide.globals.get('_editor_state');
      const proxy = fn();
      fn.destroy?.();
      const js = proxy.toJs({ dict_converter: Object.fromEntries });
      proxy.destroy?.();
      return js;
    }

    function refresh() {
      state = pullState();
      if (!state) return;
      transform = drawEditor(root.canvas, state);
      renderSidebar(state, patternNames());
      markDirty();
    }

    function markDirty() {
      if (!root.autosaveToggle?.checked) {
        root.savedStamp.textContent = 'unsaved';
        return;
      }
      clearTimeout(autosaveTimer);
      autosaveTimer = setTimeout(() => {
        // Browser can't write next to the SVG — fall through to the
        // download flow. The toggle is still meaningful: when ON, the
        // browser silently prepares the latest JSON and shows "saved at"
        // (a real save would land in the desktop editor).
        root.savedStamp.textContent = `prepared ${new Date().toLocaleTimeString()}`;
      }, 500);
    }

    function renderSidebar(s, patternList) {
      root.sidebar.innerHTML = '';
      const header = document.createElement('header');
      header.className = 'editor-header';
      // file.name flows in from the OS file picker; use textContent /
      // explicit DOM construction so a maliciously-named file (e.g.
      // `<img src=x onerror=…>.svg`) can't execute as HTML.
      const nameEl = document.createElement('strong');
      nameEl.textContent = file.name;
      const countsEl = document.createElement('span');
      countsEl.className = 'editor-counts';
      countsEl.textContent =
        `${s.components.length} component${s.components.length === 1 ? '' : 's'}, `
        + `${s.bodies.length} bod${s.bodies.length === 1 ? 'y' : 'ies'}`;
      header.append(nameEl, countsEl);
      root.sidebar.appendChild(header);

      const list = document.createElement('ul');
      list.className = 'editor-bodies';
      for (const body of s.bodies) {
        const li = document.createElement('li');
        li.className = 'editor-body';
        if (s.selected_body_id === body.id) li.classList.add('is-selected');

        const swatch = document.createElement('input');
        swatch.type = 'color';
        swatch.value = body.color_hex || '#888888';
        swatch.title = 'Body color';
        swatch.addEventListener('input', () =>
          mutate(`_editor_project.set_body_color(${body.id}, "${swatch.value}")`)
        );

        const name = document.createElement('span');
        name.className = 'editor-body-name';
        name.textContent = body.name;
        name.title = `components ${body.component_ids.join(', ')}`;
        name.addEventListener('click', () => {
          mutate(`_editor_project.select_body(${body.id})`);
        });

        const enable = document.createElement('input');
        enable.type = 'checkbox';
        enable.checked = body.enabled;
        enable.title = 'Include in pyramid pack';
        enable.addEventListener('change', () =>
          mutate(`_editor_project.set_body_enabled(${body.id}, ${enable.checked ? 'True' : 'False'})`)
        );

        const pattern = document.createElement('select');
        for (const name of patternList) {
          const opt = document.createElement('option');
          opt.value = name;
          opt.textContent = name;
          if (name === body.pattern) opt.selected = true;
          pattern.appendChild(opt);
        }
        pattern.addEventListener('change', () =>
          mutate(`_editor_project.set_body_pattern(${body.id}, "${pattern.value}")`)
        );

        const override = document.createElement('input');
        override.type = 'number';
        override.step = '0.1';
        override.placeholder = 'size';
        override.title = 'Per-body pyramid_size override (leave blank to use global)';
        override.value = body.pyramid_overrides?.pyramid_size ?? '';
        override.addEventListener('change', () => {
          const v = override.value.trim();
          if (v === '') {
            mutate(`_editor_project.set_body_pyramid_overrides(${body.id}, {})`);
          } else {
            mutate(
              `_editor_project.set_body_pyramid_overrides(${body.id}, {"pyramid_size": ${Number(v)}})`
            );
          }
        });

        li.append(swatch, name, enable, pattern, override);
        list.appendChild(li);
      }
      root.sidebar.appendChild(list);

      const actions = document.createElement('div');
      actions.className = 'editor-actions';
      const group = document.createElement('button');
      group.textContent = 'Group selected';
      group.disabled = s.selected_component_ids.length < 2;
      group.addEventListener('click', () =>
        mutate('_editor_project.group_selected_into_new_body()')
      );
      const clearSel = document.createElement('button');
      clearSel.textContent = 'Clear selection';
      clearSel.disabled = s.selected_component_ids.length === 0;
      clearSel.addEventListener('click', () =>
        mutate('_editor_project.clear_selection()')
      );
      actions.append(group, clearSel);
      root.sidebar.appendChild(actions);
    }

    // SAFETY: pythonExpr is built via template literals at the call sites
    // (renderSidebar). The interpolated values are all type-coerced to safe
    // Python literals: body.id is an int from to_editor_dict; color values
    // come from <input type=color> (browser-constrained to #rrggbb); pattern
    // values come from a <select> populated from Python; the size override
    // is run through Number() before interpolation; enable.checked is mapped
    // to literal 'True'/'False'. ANY new string-valued interpolation here
    // must either be sanitized or routed through pyodide.globals.set so the
    // value never appears in raw Python source — otherwise a body name (or
    // similar future input) containing a quote/newline becomes Python
    // syntax injection.
    async function mutate(pythonExpr) {
      try {
        await pyodide.runPythonAsync(pythonExpr);
        refresh();
      } catch (e) {
        log(`[editor] mutation failed: ${e.message || e}`);
      }
    }

    handlers.click = (event) => {
      if (!transform) return;
      const rect = root.canvas.getBoundingClientRect();
      const px = event.clientX - rect.left;
      const py = event.clientY - rect.top;
      const [svgX, svgY] = transform.fromPixel(px, py);
      // hit_test returns a Python int (auto-converted to JS number) or
      // None (auto-converted to undefined). Primitives don't allocate a
      // PyProxy; nothing to destroy. If the API ever returns a proxy,
      // the runtime would throw on the comparison below — make the
      // assumption explicit so future drift is caught.
      const hit = pyodide.runPython(`_editor_project.hit_test(${svgX}, ${svgY})`);
      if (hit !== null && hit !== undefined && typeof hit !== 'number') {
        // Defensive: destroy if Pyodide unexpectedly returned a proxy.
        hit.destroy?.();
        log(`[editor] hit_test returned unexpected type ${typeof hit}; ignoring`);
        return;
      }
      if (hit === null || hit === undefined) {
        if (!event.shiftKey) mutate('_editor_project.clear_selection()');
        return;
      }
      if (event.shiftKey) mutate(`_editor_project.shift_select_component(${hit})`);
      else mutate(`_editor_project.select_component(${hit})`);
    };
    root.canvas.addEventListener('click', handlers.click);

    handlers.save = async () => {
      const json = await pyodide.runPythonAsync('_editor_project.to_json()');
      const blob = new Blob([json], { type: 'application/json' });
      triggerDownload(blob, file.name.replace(/\.svg$/i, '') + '.project.json');
      root.savedStamp.textContent = `downloaded ${new Date().toLocaleTimeString()}`;
    };
    root.downloadBtn.addEventListener('click', handlers.save);

    async function renderEnabledBodies() {
      // Build {body_id: scad_text} on the Python side, then iterate in JS
      // because openscad-wasm calls have to be sequential (one shared WASM
      // instance with stateful FS).
      pyodide.runPython('_editor_per_body_scads = generate_per_body_scads(_editor_project, _editor_cache)');
      const scadProxy = pyodide.globals.get('_editor_per_body_scads');
      const scadByBody = scadProxy.toJs({ dict_converter: Object.fromEntries });
      scadProxy.destroy?.();
      const stlByBody = {};
      for (const [bidStr, scad] of Object.entries(scadByBody)) {
        const bid = Number(bidStr);
        log(`[editor] rendering body ${bid}…`);
        const t0 = performance.now();
        try {
          stlByBody[bid] = await renderStl(scad);
          log(`[editor] body ${bid}: ${(stlByBody[bid].length / 1024).toFixed(1)} KB in ${(performance.now() - t0).toFixed(0)}ms`);
        } catch (e) {
          log(`[editor] body ${bid} FAILED: ${e.message || e}`);
        }
      }
      return stlByBody;
    }

    function buildPythonStlDict(stlByBody) {
      // Hand a Python dict {int: bytes} to the exporters. Pyodide's
      // toPy automatically converts Uint8Array → bytes.
      const py = pyodide.toPy(stlByBody);
      pyodide.globals.set('_editor_stl_by_body', py);
      return py;
    }

    function deletePythonGlobals(names) {
      // Wrap in try/except so a missing global (never created) doesn't
      // raise on cleanup. Releases the WASM heap held by per-body STLs.
      const expr = names
        .map((n) => `try:\n    del ${n}\nexcept NameError:\n    pass`)
        .join('\n');
      try { pyodide.runPython(expr); } catch { /* best effort */ }
    }

    handlers.exportStl = async () => {
      let stlByBody;
      try {
        stlByBody = await renderEnabledBodies();
      } catch (e) {
        log(`[editor] STL set export aborted: ${e.message || e}`);
        return;
      }
      if (Object.keys(stlByBody).length === 0) {
        log('[editor] nothing to export (no enabled bodies)');
        return;
      }
      const pyDict = buildPythonStlDict(stlByBody);
      try {
        pyodide.runPython(
          'from stomppad.exporters import build_stl_set\n'
          + '_editor_stl_bundle = build_stl_set(_editor_project, _editor_stl_by_body)'
        );
        const bundleProxy = pyodide.globals.get('_editor_stl_bundle');
        const bundle = bundleProxy.toJs({ dict_converter: Object.fromEntries });
        bundleProxy.destroy?.();
        // Convert each {filename: bytes} entry to a Uint8Array (Pyodide
        // returns bytes as Uint8Array already; strings stay as strings).
        const zipEntries = {};
        for (const [name, content] of Object.entries(bundle)) {
          zipEntries[name] = content;
        }
        const zipBytes = buildZip(zipEntries);
        triggerDownload(
          new Blob([zipBytes], { type: 'application/zip' }),
          file.name.replace(/\.svg$/i, '') + '-stl-set.zip'
        );
        root.savedStamp.textContent = `STL set: ${Object.keys(stlByBody).length} bodies`;
      } catch (e) {
        log(`[editor] STL set export failed: ${e.message || e}`);
      } finally {
        pyDict.destroy?.();
        // Free the WASM-heap-resident per-body STL bytes + intermediate
        // bundle / scads now that the download is queued.
        deletePythonGlobals([
          '_editor_per_body_scads',
          '_editor_stl_by_body',
          '_editor_stl_bundle',
        ]);
      }
    };
    root.exportStlBtn.addEventListener('click', handlers.exportStl);

    handlers.export3mf = async () => {
      let stlByBody;
      try {
        stlByBody = await renderEnabledBodies();
      } catch (e) {
        log(`[editor] 3MF export aborted: ${e.message || e}`);
        return;
      }
      if (Object.keys(stlByBody).length === 0) {
        log('[editor] nothing to export (no enabled bodies)');
        return;
      }
      const pyDict = buildPythonStlDict(stlByBody);
      try {
        pyodide.runPython(
          'from stomppad.exporters import build_threemf\n'
          + '_editor_threemf_bytes = build_threemf(_editor_project, _editor_stl_by_body)'
        );
        const blobProxy = pyodide.globals.get('_editor_threemf_bytes');
        const bytes = blobProxy.toJs();
        blobProxy.destroy?.();
        triggerDownload(
          new Blob([bytes], { type: 'model/3mf' }),
          file.name.replace(/\.svg$/i, '') + '.3mf'
        );
        root.savedStamp.textContent = `3MF: ${Object.keys(stlByBody).length} bodies`;
      } catch (e) {
        log(`[editor] 3MF export failed: ${e.message || e}`);
      } finally {
        pyDict.destroy?.();
        deletePythonGlobals([
          '_editor_per_body_scads',
          '_editor_stl_by_body',
          '_editor_threemf_bytes',
        ]);
      }
    };
    root.export3mfBtn.addEventListener('click', handlers.export3mf);

    handlers.toggle = () => {
      if (root.autosaveToggle.checked) {
        root.savedStamp.textContent = 'auto-prepare on';
      } else {
        root.savedStamp.textContent = 'manual';
      }
    };
    root.autosaveToggle.addEventListener('change', handlers.toggle);

    return {
      refresh,
      state: () => state,
      dispose() {
        root.canvas.removeEventListener('click', handlers.click);
        root.downloadBtn.removeEventListener('click', handlers.save);
        root.autosaveToggle.removeEventListener('change', handlers.toggle);
        root.exportStlBtn.removeEventListener('click', handlers.exportStl);
        root.export3mfBtn.removeEventListener('click', handlers.export3mf);
        clearTimeout(autosaveTimer);
        root.sidebar.innerHTML = '';
        const ctx = root.canvas.getContext('2d');
        ctx.clearRect(0, 0, root.canvas.width, root.canvas.height);
        // Release the WASM-heap-resident project + cache + intermediates
        // so opening a second file doesn't accumulate prior projects in
        // Pyodide's heap.
        deletePythonGlobals([
          '_editor_project',
          '_editor_cache',
          '_editor_components',
          '_editor_svg_info',
          '_editor_parsed',
          '_editor_state',
          '_editor_pattern_names',
          '_editor_per_body_scads',
          '_editor_stl_by_body',
          '_editor_stl_bundle',
          '_editor_threemf_bytes',
          '_editor_filename',
        ]);
      },
    };
  }

  function triggerDownload(blob, name) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }

  function renderEmpty(message = 'Pick an SVG to edit.') {
    if (editorHandle) editorHandle.dispose();
    editorHandle = null;
    const ctx = root.canvas.getContext('2d');
    ctx.clearRect(0, 0, root.canvas.width, root.canvas.height);
    // textContent on a single <p> — message may include file.name (from
    // the failed-parse path); plain text avoids the same XSS vector the
    // sidebar header was vulnerable to.
    root.sidebar.innerHTML = '';
    const p = document.createElement('p');
    p.className = 'editor-empty';
    p.textContent = message;
    root.sidebar.appendChild(p);
    root.savedStamp.textContent = '';
  }

  root.fileInput.addEventListener('change', () => {
    const file = root.fileInput.files[0];
    if (file) loadSvgFile(file);
  });

  renderEmpty();

  return {
    dispose() {
      if (editorHandle) editorHandle.dispose();
      editorHandle = null;
    },
  };
}
