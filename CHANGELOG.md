# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

When you tag a release with `v*`, the [build workflow](.github/workflows/build.yml)
publishes Windows / macOS / Linux binaries automatically. Copy the relevant
section below into the GitHub release body.

## [Unreleased]

### Added — v2 (interactive editor + multicolor export)

- **`stomppad/` package** (Phase 0). The geometry pipeline moved from
  the single `pyramid_position_calculator.py` file into a multi-module
  package: `geometry`, `packing`, `patterns/`, `project`, `cache`,
  `openscad`, `exporters/`. The top-level `.py` is now a back-compat
  shim. Web sync via `web/scripts/prepare.js` emits
  `public/stomppad-manifest.json` instead of copying one file.
- **Component-aware SVG parsing** (Phase 1.1, `stomppad/geometry.py`).
  Each subpath / rect / polygon / circle / ellipse becomes its own
  `Component`. Even-odd nesting decides which loops are holes vs.
  nested bodies. Near-full-canvas rectangular backgrounds auto-drop
  (≥95% coverage AND ≥0.999 bbox-fill); single-rect pads survive the
  guard. Source `fill` captured per component as the body-color default.
- **Hardened packer** (Phase 1.2, `stomppad/packing.py`). Skeleton +
  footprint + hex placement extracted from the monolith. Validation
  uses `shapely.prepared.prep(...)` batch contains tests (speedup +
  thin-neck correctness fix). New `pack_component` packs a single
  `Component` (holes subtracted); `PackContext` caches per-component
  skeletons so the editor's re-pack-on-edit avoids the slow medial-axis
  step for untouched bodies.
- **Pattern registry** (Phase 1.3, `stomppad/patterns/`). Pluggable
  `PatternStrategy` protocol + four built-in strategies:
  `skeleton` (the legacy default), `hexagonal` (uniform hex without
  skeleton rotation), `rectangular` (square grid), `triangular`
  (60°-offset rows). Bodies reference strategies by name.
- **`ShapeProject` model** (Phase 1.4, `stomppad/project.py`). Versioned
  JSON contract (`schema_version=1`) carrying `components`, `bodies`,
  `global_params`, and `svg_info`. `Body` has `id`, `name`,
  `component_ids`, `color_hex`, `enabled`, `pattern`, and
  `pyramid_overrides`. `to_json` / `from_json` plus a versioned
  `from_dict` that rejects sub-1 and newer-than-supported schemas.
- **Stateful editor API** (Phase 2.1, `stomppad/project.py`). Selection
  lives on the project (`selected_component_ids`, `selected_body_id`,
  both ephemeral — not serialized). Mutators: `select_component`,
  `shift_select_component`, `clear_selection`, `assign_components_to_body`,
  `group_selected_into_new_body`, `toggle_body_enabled`,
  `set_body_color`, `set_body_pattern`, `set_body_pyramid_overrides`,
  `update_global_params`, `flip_hole_to_body`, `demote_body_to_hole`.
  Observer hook (`on_change` / `off_change`) emits one of four event
  types so consumers (the cache, the UI) can react selectively.
  `hit_test(x, y)` returns the component under a click (None on hole /
  miss; nested-body-in-hole wins via smallest-area-first iteration).
- **Per-body output cache** (Phase 2.1, `stomppad/cache.py`).
  `BodyOutputCache` subscribes to project events on construction; lazily
  packs each body via `pack_component`; selectively invalidates on
  body-changed events; clears all on topology / global-param changes;
  ignores selection events.
- **Sidecar JSON** (Phase 2.1). `ShapeProject.save_sidecar(svg_path)`
  writes `<svg-stem>.project.json` atomically (tmp + rename — POSIX
  rename is atomic, Windows is transactional in the common case).
  `load_sidecar` reads it back. Catches Windows `PermissionError` when
  the file is locked by a slicer, returning `None` for the auto-save
  loop to retry on the next tick.
- **Web editor** (Phase 2.2, `web/src/editor.js` + `preview-2d.js`).
  Section 4 of the index page: pick one SVG, the canvas renders each
  component colored by its body, click to select (shift to multi-),
  right sidebar has a swatch / enable checkbox / pattern dropdown /
  size-override entry per body, "Group selected" and "Clear selection"
  buttons, "Download .project.json" button, optional auto-prepare
  toggle. File-name handling uses `textContent` (not `innerHTML`) to
  avoid XSS via maliciously-named SVGs.
- **Desktop Edit tab** (Phase 2.3, `desktop_editor.py`). Same model,
  same events, tkinter widgets: `Canvas` for the preview (holes
  approximated as white overlays — tkinter can't draw real polygon
  holes; the underlying `ShapeProject` knows the true geometry),
  `colorchooser` for the swatch, `ttk.Combobox` for the pattern.
  Debounced sidecar auto-save (500 ms) with a checkbox to disable.
  Loading an SVG with an existing sidecar reads it back only if the
  sidecar's component ids match the freshly-parsed SVG (stale-sidecar
  guard); otherwise warns + regenerates default grouping.
- **Multi-body OpenSCAD** (Phase 3.1, `stomppad/openscad.py`).
  `generate_body_scad(project, body, positions)` produces a
  self-contained SCAD per body: inlines geometry as
  `polygon(points=…, paths=…)` (no `import(svg_file)` dependency),
  union+difference assembly mirrors the legacy single-shape pad for
  base + raised rim. `generate_per_body_scads(project, cache)` returns
  `{body_id: scad}` for enabled bodies. Legacy
  `generate_openscad_with_positions` moved into the same module
  unchanged (regression fixture stays byte-identical).
- **STL-set exporter** (Phase 3.2, `stomppad/exporters/stl_set.py`).
  `build_stl_set(project, stl_per_body)` returns a `{filename: bytes}`
  bundle — one slug-named `.stl` per enabled body plus a
  `print-guide.txt` mapping body → color → file. `write_stl_set`
  writes the bundle to a directory.
- **Generic 3MF exporter** (Phase 3.3, `stomppad/exporters/threemf.py`).
  `build_threemf(project, stl_per_body)` produces a `.3mf` zip (hand-
  rolled, no new deps) with `[Content_Types].xml` + `_rels/.rels` +
  `3D/3dmodel.model`. One `<object>` per body with `basematerials`
  `displaycolor` from `body.color_hex`. Binary STL parser uses
  structural length-match detection (`len == 84 + count * 50`) so
  binary STLs with header bytes matching ASCII-style strings parse
  correctly. Optional `logger` callback warns on zero-triangle bodies.
- **Export UI in both frontends** (Phase 3.4). Web editor gets two
  buttons: "Export STL set (.zip)" (hand-rolled JS zip writer in
  `web/src/zip.js` — no new dep) and "Export .3mf". Desktop Edit tab
  gets the same two buttons, invoking subprocess OpenSCAD per body in
  a tempdir; on Windows the tempdir cleanup is `try/except
  PermissionError`-guarded for the case where OpenSCAD's CGAL child
  hasn't fully released a file handle.
- **pytest suite** under `tests/` (~74 tests across 4 files). Includes
  the Phase 0 byte-equality regression, Phase 1 component/pack/JSON
  acceptance, Phase 2 selection + mutations + cache + sidecar +
  observer, Phase 3 SCAD generation + STL set + 3MF zip.
- **`requirements-dev.txt`** — pinned `numpy` / `scikit-image` /
  `shapely` for the byte-equality regression + `pytest`.

### Changed — v2

- Desktop `bulk_processor_gui.py` imports from `stomppad` directly
  (Phase 1). The shim still works for `import
  pyramid_position_calculator` and `python pyramid_position_calculator.py`.
- `pyramid_position_calculator.py` is now a 19-line shim; the
  implementation lives in `stomppad/`.
- `bulk_processor.spec` uses `collect_submodules('stomppad')` so the
  frozen binary picks up new submodules without spec edits, plus
  explicit `desktop_editor` and `pyramid_position_calculator`
  hiddenimports.
- `docs/architecture.md` rewritten for the v2 package + editor + export
  layers.

## [0.1.0] — 2026-04-24

First public-ready release. The project now ships two interfaces backed by
one shared geometry pipeline (`pyramid_position_calculator.py`), with
prebuilt cross-platform binaries.

### Added

- **Browser version** under `web/` (Vite + Pyodide + openscad-wasm). The
  same Python pipeline runs in WebAssembly. Multi-file SVG upload, per-file
  status pills, mirrored Python console, downloadable `.scad` / `.svg` /
  `.stl`, in-page 2D Canvas debug preview (polygon outline + skeleton +
  pyramid placements), three.js STL viewer (orbit controls). Deployable to
  Vercel by setting Root Directory to `web/`. See
  [`web/README.md`](web/README.md).
- **Pre-built desktop binaries** for Windows / macOS / Linux via PyInstaller
  + a GitHub Actions matrix workflow. Tagged `v*` releases publish zipped
  artifacts. See [`bulk_processor.spec`](bulk_processor.spec) and
  [`.github/workflows/build.yml`](.github/workflows/build.yml).
- **Logger callback** (`logger=`) on every public function in
  `pyramid_position_calculator.py`. Lets the desktop GUI surface calculator
  output in its Console tab and the browser surface it in the on-page
  console; standalone use still prints to stdout.
- **Preview-debug mode** in the desktop GUI: render the matplotlib debug
  visualization and pause for per-file approval.
- **Threads spinbox** in the desktop GUI: controls both the SVG worker
  process count and the STL render thread count.
- **Stop button** in the web UI: aborts between files (cannot interrupt
  mid-Pyodide call without Web Workers).
- SVG element parsing now also handles `<polygon>`, `<rect>`, `<circle>`,
  `<ellipse>` in addition to `<path>` (Figma / icon-set exports).
- `CONTRIBUTING.md` (public-repo guide) and a `docs/` folder
  ([desktop guide](docs/desktop.md), [architecture](docs/architecture.md)).

### Changed

- **Desktop bulk processor is now actually parallel.** SVG work runs in a
  `ProcessPoolExecutor` (true parallelism, bypasses GIL); STL rendering
  runs in a separate `ThreadPoolExecutor` so OpenSCAD subprocesses overlap
  with the next files' geometry pass. Worker logs flow back through a
  `multiprocessing.Manager` queue. Preview-debug mode falls back to a
  single-process sequential loop (the preview dialog can't cross process
  boundaries).
- Slim `README.md` as a front door; deep documentation lives in `docs/`.
- `calculate_valid_pyramid_positions` now accepts `skeleton_points=` and
  `skeleton_resolution=` parameters. Callers pre-compute the skeleton
  once and pass it in, eliminating a redundant skeleton recomputation per
  file.
- openscad-wasm upgraded from the GitHub releases (last tagged 2022.03.20,
  CGAL-only) to the official OpenSCAD playground build
  (`files.openscad.org/playground/...`, 2025.03.25, manifold backend) which
  handles complex pad + pyramid geometry that the older CGAL-only build
  asserted on (`CGAL/Nef_3/SNC_external_structure.h:1144`).
- Generated `.scad` template no longer emits a redundant base extrude
  (it was fully contained in the outline-difference) and sinks pyramids
  0.01 mm into the base. Cleaner geometry for any backend.
- `stop_requested` migrated from a plain bool to `threading.Event` for
  clean cross-thread signalling.

### Fixed

- `save_debug_visualization` no longer crashes on `MultiPolygon` results
  (SVGs with multiple disjoint shapes). Iterates over both `Polygon` and
  `MultiPolygon` exterior rings.
- 2D web preview also handles `MultiPolygon` (one path per ring).
- Preview pipeline aborts cleanly when openscad-wasm throws; the surfaced
  message now includes the actual buffered stderr instead of an
  Emscripten exception pointer.
- openscad-wasm 2025 build requires explicit `noInitialRun: true` (the
  2022 wrapper set it as a default; the 2025 monolithic loader doesn't).
  Without it, the instance aborts at init and every `callMain` throws
  "program has already aborted!"
- Three.js STL viewers no longer leak WebGL contexts on Clear / re-process.
  Browsers cap at ~16 contexts; the leak silently broke the 3D preview
  after a few cycles.
- PyProxy lifecycles in the web pipeline are explicitly destroyed instead
  of relying on Pyodide GC.
- openscad-wasm virtual FS now sweeps stale `.svg` files between renders
  so processing different filenames in sequence doesn't accumulate them.
- Blob-URL revoke timeout extended from 1 s to 60 s — slow connections /
  Save-As dialogs no longer race the revoke.
- CI pip cache now correctly points at `requirements_bulk_processor.txt`
  instead of failing on the missing default `requirements.txt`.

### Performance

- `calculate_skeleton` vectorized via `shapely.contains_xy` over a
  meshgrid (50–100× faster than the prior per-pixel `polygon.contains(Point)`
  loop). Matters disproportionately in Pyodide where Python loops are
  uncached interpreted bytecode.
- Skeleton no longer recomputed twice per file (callers pass the
  pre-computed result into `calculate_valid_pyramid_positions`).
- `np.linalg.eig` → `np.linalg.eigh` in `calculate_centerline_tangent`
  (faster + numerically stable on symmetric matrices).
- Web pipeline split into multiple `runPythonAsync` calls so console +
  repaints flush between pipeline steps instead of freezing for the
  whole run.
- Three.js bundle dynamic-imported in the browser — users without STL
  preview never download it.

### Security

- Prefer `defusedxml.ElementTree` if installed when parsing SVG XML (falls
  back to stdlib `xml.etree`, which since Python 3.7.1 disables external
  entity expansion by default). Added `defusedxml>=0.7.1` to the desktop
  requirements.

### Removed

- Unused imports (`os`, `sys`, `unary_union`, `voronoi_diagram`,
  `LineString`, `MultiPoint`, `Path`, `scipy.spatial.distance`,
  `skeletonize`).

[Unreleased]: https://github.com/printwithmexyz/stomp-pad-generator/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/printwithmexyz/stomp-pad-generator/releases/tag/v0.1.0
