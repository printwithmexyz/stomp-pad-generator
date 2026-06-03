# Architecture

This doc is for contributors. End users want
[desktop.md](desktop.md) or [web/README.md](../web/README.md).

## Single source of truth

```
stomppad/                            (Python package — single source of truth)
        │
        ├── re-exported by → pyramid_position_calculator.py   (shim, kept for
        │                                                      back-compat)
        ├── used by        → bulk_processor_gui.py    (desktop; PyInstaller
        │                                              picks up submodules via
        │                                              collect_submodules)
        └── synced into web/public/stomppad-manifest.json at build
            → unpacked into Pyodide's FS by web/src/main.js
```

The whole geometry pipeline (SVG → polygon → medial-axis skeleton →
hex-packed pyramid placement → OpenSCAD source) lives in the `stomppad`
package. Both interfaces import the same functions; nothing is reimplemented
per platform.

Phase 0 packaged the previous monolithic `pyramid_position_calculator.py`
into `stomppad/`. The top-level file is now a thin re-export shim, so
`import pyramid_position_calculator` and `python pyramid_position_calculator.py`
keep working unchanged for existing callers and the standalone `main()`.

Phase 1.1 split SVG parsing out into `stomppad.geometry`. Each path
subpath / rect / polygon / circle / ellipse becomes its own ring, even-odd
nesting decides which rings are holes vs. nested bodies, and a heuristic
(>=95% viewBox coverage AND bbox-fill >=0.999) drops near-full-canvas
rectangular backgrounds. The original `parse_svg_to_polygon` is now a
thin shim over `parse_svg_to_components` + `unary_union` so existing
single-polygon callers (the desktop packer, the web preview) keep
working. Phase 1.5's test suite is where the multi-ring assertions land.

Phase 1.4 added `stomppad.project` — the `ShapeProject` dataclass that
both frontends will round-trip through in Phase 2. Schema is versioned
from day one (`SCHEMA_VERSION = 1`) so sidecar `.project.json` files that
land on users' disks in Phase 2.3 can be migrated rather than rejected by
future builds.

Phase 1.2 / 1.3 split skeleton, footprint, and hex placement out into
`stomppad.packing`, and introduced `stomppad.patterns` — a registry of
placement strategies (skeleton-following / uniform hex / rectangular /
triangular). `pack_component(component, ..., pattern="hexagonal")`
resolves the strategy by name and runs the shared footprint validation
(prepared-geometry `contains` tests for the speedup and thin-neck fix
called out in the plan). `PackContext` caches per-component skeletons so
Phase 2's "re-pack only edited bodies" can avoid recomputing untouched
medial axes — the slow step in Pyodide.

### Web sync (manifest, not zip)

`web/scripts/prepare.js` walks the `stomppad/` directory and writes
`web/public/stomppad-manifest.json` — a JSON object of
`{files: [{path, content}]}` with every `.py` source inlined. `main.js`
fetches the manifest once, writes each entry to Pyodide's virtual FS under
`/stomppad/`, then imports the package.

The v2 plan originally proposed a `.zip` archive, but Node has no built-in
zip writer and the plan also required "no new dep / ~15 lines"; a JSON
manifest satisfies both constraints with no archive library on either side.
Pyodide's `unpackArchive('zip')` could replace this later if the package
ever grows binary assets — the wire format is the only thing that would
change, not the contract.

## Logger callback pattern

Every public function in `stomppad` (and therefore the
`pyramid_position_calculator` shim) accepts an optional
`logger=` callable:

```python
def parse_svg_to_polygon(svg_file, ..., logger=None):
    _log(logger, f"  SVG parsed: {len(all_points)} points sampled")
```

`_log(logger, msg)` calls `logger(msg)` if provided, else falls back to
`print`. This is what lets the desktop GUI surface calculator output in its
Console tab and the browser surface it in its on-page console — both pass
their own callback. Standalone use (`python pyramid_position_calculator.py`
via the shim, or `python -m stomppad` once a `__main__.py` is added) still
prints to stdout.

## Desktop concurrency model

`bulk_processor_gui.py` runs the GUI on the tkinter main thread and the
processing pipeline in a coordinator thread. The coordinator picks a path:

| Condition | Path |
|---|---|
| `preview_debug` on, **or** `num_threads == 1` | Sequential in-process loop. Preview dialog needs `self`. |
| Otherwise | `ProcessPoolExecutor` (SVG work) + `ThreadPoolExecutor` (STL render). |

Why two pools: SVG processing is CPU-bound Python (skeleton calculation
holds the GIL), so it needs separate processes. STL rendering is
`subprocess.run` against OpenSCAD — it releases the GIL — so threads
overlap fine and overlap with SVG worker processes for higher throughput.

Worker → GUI communication uses a `multiprocessing.Manager.Queue`. Workers
push log lines prefixed with `[file_stem]`; a pump thread in the main
process drains the queue and calls `self.log` (which schedules the actual
text-widget update via `root.after(0, ...)`).

The `multiprocessing.freeze_support()` call in `main()` is required so that
PyInstaller-frozen binaries don't fork-bomb on Windows when worker processes
re-execute the entry point.

## Web concurrency model

There isn't one. Pyodide is single-threaded — multi-file uploads run
sequentially. The reason this is acceptable for the browser path is that the
browser is the wrong tool for batches of 50; that's the desktop's job.

What does run concurrently in the browser:
- Pyodide bootstrap and openscad-wasm download happen in parallel on initial
  page load.
- three.js code-splits via dynamic `import('three')`, so users without STL
  preview never download it.

## Web ↔ Pyodide interop

On bootstrap, `web/src/main.js` fetches `stomppad-manifest.json` (produced
by `prepare.js`), writes each entry into Pyodide's FS at `/stomppad/<path>`,
then imports the package. At process time it writes the uploaded SVG to
`/input.svg`, sets the parameter dict via `pyodide.toPy(params)` as a
global, then calls into the calculator. The Python logger callback marshals
strings back to the JS `log()` function for the on-page console.

After processing, preview data is materialized into a Python dict
(polygon exterior rings, skeleton points, valid positions) and pulled to JS
via `pyodide.globals.get('preview_data').toJs({ dict_converter: Object.fromEntries })`.
The 2D canvas draws from that data; the 3D canvas (when STL is enabled)
parses the openscad-wasm STL output via three.js's `STLLoader`.

## openscad-wasm loader gotcha

Vite forbids source-code `import()` against files in `/public`. The renderer
in `web/src/scad-renderer.js` works around this by injecting a
`<script type="module">` at runtime whose inline `import()` Vite never sees.
The browser still resolves the URL against its real location, so the
loader's `import.meta.url`-relative fetches for sibling `.wasm.js` and
`.wasm` files resolve correctly.

## Packaging

`bulk_processor.spec` is a single PyInstaller spec that produces:

- Windows → windowed `.exe`
- Linux → bin
- macOS → `.app` bundle (the `BUNDLE` block is gated on `sys.platform == 'darwin'`)

`.github/workflows/build.yml` runs the spec on a `windows-latest` /
`macos-latest` / `ubuntu-latest` matrix, uploads each artifact, and on `v*`
tags zips them into a release.

OpenSCAD itself is **not** bundled — license + size. End users still install
it themselves if they want STL rendering.

## File map

```
stomppad/
├── __init__.py                   parse_svg_to_polygon shim + OpenSCAD
│                                 generation + standalone main()
├── geometry.py                   Phase 1.1 — Component, parse_svg_to_components
│                                 (multi-ring, hole-aware, background drop)
├── packing.py                    Phase 1.2 — skeleton, footprint, hex
│                                 placement; pack_component + PackContext
│                                 cache; prepared-geometry contains tests
├── patterns/                     Phase 1.3 — pluggable placement strategies
│   ├── __init__.py               PatternStrategy protocol + registry
│   ├── skeleton.py               skeleton-following hex (default)
│   ├── hexagonal.py              uniform hex, no rotation
│   ├── rectangular.py            square grid
│   └── triangular.py             triangular lattice
└── project.py                    Phase 1.4 — Body, ShapeProject, schema_v1
                                  JSON contract for both frontends
pyramid_position_calculator.py    re-export shim → stomppad (back-compat)
bulk_processor_gui.py             tkinter GUI + process pool + STL queue
bulk_processor.spec               PyInstaller spec (used by CI)
.github/workflows/build.yml       cross-platform binary builds + releases
tests/
├── fixtures/sample.svg           single-shape regression fixture
├── fixtures/sample_golden.scad   captured pre-refactor scad output
├── fixtures/_capture_golden.py   one-shot recapture script (manual)
└── test_regression.py            asserts byte-identical scad output
requirements-dev.txt              pytest (for tests/)
web/
├── index.html                    upload form + console + result cards
├── package.json                  vite + three (Pyodide loaded from CDN)
├── scripts/prepare.js            build stomppad-manifest.json + fetch openscad-wasm
└── src/
    ├── main.js                   Pyodide bootstrap + processing loop
    ├── scad-renderer.js          openscad-wasm wrapper (Vite workaround)
    ├── preview-2d.js             Canvas2D debug viz
    ├── preview-3d.js             three.js STL viewer (lazy-imported)
    └── style.css
docs/
├── desktop.md                    end-user guide for the GUI
├── v2-plan.md                    multicolor / interactive editor roadmap
└── architecture.md               this file
```

## See also

- [../README.md](../README.md)
- [desktop.md](desktop.md)
- [../web/README.md](../web/README.md)
