# Architecture

This doc is for contributors. End users want
[desktop.md](desktop.md) or [web/README.md](../web/README.md).

## Single source of truth

```
pyramid_position_calculator.py
        │
        ├── used by → bulk_processor_gui.py    (desktop, runs in worker processes)
        └── synced into web/public/ at build → loaded by Pyodide in the browser
```

The whole geometry pipeline (SVG → polygon → medial-axis skeleton →
hex-packed pyramid placement → OpenSCAD source) lives in one Python module.
Both interfaces import the same functions; nothing is reimplemented per
platform. The web project's `scripts/prepare.js` copies the parent file into
`web/public/pyramid_position_calculator.py` before every `dev` and `build`
so the browser and desktop never drift.

## Logger callback pattern

Every public function in `pyramid_position_calculator.py` accepts an optional
`logger=` callable:

```python
def parse_svg_to_polygon(svg_file, ..., logger=None):
    _log(logger, f"  SVG parsed: {len(all_points)} points sampled")
```

`_log(logger, msg)` calls `logger(msg)` if provided, else falls back to
`print`. This is what lets the desktop GUI surface calculator output in its
Console tab and the browser surface it in its on-page console — both pass
their own callback. Standalone use (`python pyramid_position_calculator.py`)
still prints to stdout.

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

`web/src/main.js` writes the uploaded SVG to Pyodide's virtual FS as
`/input.svg`, sets the parameter dict via `pyodide.toPy(params)` as a global,
then calls into the calculator. The Python logger callback marshals strings
back to the JS `log()` function for the on-page console.

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
pyramid_position_calculator.py    geometry pipeline + standalone main()
bulk_processor_gui.py             tkinter GUI + process pool + STL queue
bulk_processor.spec               PyInstaller spec (used by CI)
.github/workflows/build.yml       cross-platform binary builds + releases
web/
├── index.html                    upload form + console + result cards
├── package.json                  vite + three (Pyodide loaded from CDN)
├── scripts/prepare.js            sync calc + download openscad-wasm
└── src/
    ├── main.js                   Pyodide bootstrap + processing loop
    ├── scad-renderer.js          openscad-wasm wrapper (Vite workaround)
    ├── preview-2d.js             Canvas2D debug viz
    ├── preview-3d.js             three.js STL viewer (lazy-imported)
    └── style.css
docs/
├── desktop.md                    end-user guide for the GUI
└── architecture.md               this file
```

## See also

- [../README.md](../README.md)
- [desktop.md](desktop.md)
- [../web/README.md](../web/README.md)
