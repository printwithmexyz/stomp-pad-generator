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

const PATTERN_OPTIONS = ['skeleton', 'hexagonal', 'rectangular', 'triangular'];

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
    let dirty = false;

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
      transform = drawEditor(root.canvas, state, { pyramidSize: 4 });
      renderSidebar(state, patternNames());
      markDirty();
    }

    function markDirty() {
      if (!root.autosaveToggle?.checked) {
        root.savedStamp.textContent = 'unsaved';
        return;
      }
      dirty = true;
      clearTimeout(autosaveTimer);
      autosaveTimer = setTimeout(() => {
        // Browser can't write next to the SVG — fall through to the
        // download flow. The toggle is still meaningful: when ON, the
        // browser silently prepares the latest JSON and shows "saved at"
        // (a real save would land in the desktop editor).
        root.savedStamp.textContent = `prepared ${new Date().toLocaleTimeString()}`;
        dirty = false;
      }, 500);
    }

    function renderSidebar(s, patternList) {
      root.sidebar.innerHTML = '';
      const header = document.createElement('header');
      header.className = 'editor-header';
      header.innerHTML = `
        <strong>${file.name}</strong>
        <span class="editor-counts">${s.components.length} component${s.components.length === 1 ? '' : 's'}, ${s.bodies.length} bod${s.bodies.length === 1 ? 'y' : 'ies'}</span>
      `;
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
      const hit = pyodide.runPython(`_editor_project.hit_test(${svgX}, ${svgY})`);
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
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = file.name.replace(/\.svg$/i, '') + '.project.json';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 30_000);
      root.savedStamp.textContent = `downloaded ${new Date().toLocaleTimeString()}`;
    };
    root.downloadBtn.addEventListener('click', handlers.save);

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
        clearTimeout(autosaveTimer);
        root.sidebar.innerHTML = '';
        const ctx = root.canvas.getContext('2d');
        ctx.clearRect(0, 0, root.canvas.width, root.canvas.height);
      },
    };
  }

  function renderEmpty(message = 'Pick an SVG to edit.') {
    if (editorHandle) editorHandle.dispose();
    editorHandle = null;
    const ctx = root.canvas.getContext('2d');
    ctx.clearRect(0, 0, root.canvas.width, root.canvas.height);
    root.sidebar.innerHTML = `<p class="editor-empty">${message}</p>`;
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
