# Architecture

This doc is for contributors. End users want
[desktop.md](desktop.md) or [web/README.md](../web/README.md).

## Single source of truth

```
stomppad/                     (Python package)
        │
        ├── used by → bulk_processor_gui.py    (desktop, runs in worker processes)
        └── synced into web/public/stomppad/ at build → loaded by Pyodide in the browser
```

The whole geometry pipeline lives in one Python package. Both interfaces call
`stomppad.pad.build_pad(svg, PadSettings)` and
`stomppad.openscad.generate_openscad(design)`; nothing is reimplemented per
platform. The web project's `scripts/prepare.js` copies every module into
`web/public/stomppad/` with a `manifest.json` before every `dev` and `build`,
and `main.js` rebuilds the package in Pyodide's filesystem from that
manifest, so the browser and desktop never drift.

| Module | Job |
|---|---|
| `svg_parse.py` | SVG → colored regions, resolved the way a browser paints it |
| `refine.py` | scale to mm, cleanup, printability warnings, drop / recolor |
| `packing.py` | medial-axis centerline and hex-packed pyramid placement |
| `pad.py` | `PadSettings`, `PadDesign`, `build_pad`: the pipeline end to end |
| `openscad.py` | a design as `.scad` text with inline polygons (no SVG import); `part` picks the whole pad or one body |
| `threemf.py` | body STLs → one 3MF with a part per filament |
| `overlay.py` | every union, difference, intersection, offset, simplify and repair, done so none can raise |
| `batch.py` | desktop only: files on disk, cache, STL and 3MF export, debug picture |

Python owns all geometry. OpenSCAD only extrudes and subtracts the polygons
it is handed, which is why coordinates can never disagree between the two.

### Bodies

A pad is split into one body per filament (`PadDesign.bodies`). Body 1 is the
base: the rim and all of the floor that no other body claims. Every other
color owns the top `color_depth` of the floor under its regions, plus the
pyramids standing there. The base takes the color covering the most area
unless `base_color` says otherwise, so a four-color logo needs four
filaments, not five.

The `.scad` has a `part` variable. `part = 0` is the whole pad as one solid.
`-D part=N` renders body N alone. Color bodies are grown 0.005 mm
(`color_overlap`) so that neighbours overlap by a hair: an exact fit would
leave slivers of base between them wherever two outlines disagree in the
last decimal. Cleanup leaves no gap between colors wider than twice that.

`threemf.py` turns the rendered body STLs into a 3MF: one mesh object per
body, joined by a components object, plus `Metadata/model_settings.config`,
which is where Bambu Studio reads each part's filament slot (`extruder`)
from. The layout follows Bambu Studio's importer
(`src/libslic3r/Format/bbs_3mf.cpp`), read at commit `930fe93`. Standard 3MF
colors are off by default because recent Bambu Studio versions respond to
them by opening a color-matching dialog and discarding the filament slots.

### What OpenSCAD drops without saying

OpenSCAD works at about 0.000015 mm. A hole or sliver near that size
collapses, the extruded mesh comes out open, and OpenSCAD leaves the shape
out of the result. It logs `ERROR: The given mesh is not closed!` and still
exits 0 with an STL. A color body can vanish this way while its pyramids
remain. The same happens when two parts of a shape touch at a single point,
as two squares meeting corner to corner do: the extrusion is not a solid any
mesh can describe, and the whole cavity cut goes missing. Two defences:

- `pad._tidy` removes anything narrower than `SMALLEST_DETAIL` (0.01 mm) from
  every shape bound for the `.scad`, thins out corners crowded a few
  thousandths apart (those trip assertions in OpenSCAD's CGAL kernel), pulls
  outlines that touch at a point a few thousandths apart, and snaps the
  result to `OUTPUT_STEP`, the precision the file is written at. Set
  operations leave such debris along every seam, so any new shape written
  into the `.scad` needs the same treatment.
- `batch.render_stl` fails the render, and deletes the STL, when OpenSCAD's
  log has an `ERROR:` line or the STL has no facets. The web renderer does
  not check yet.

### GEOS errors and Pyodide

In Pyodide a C++ exception from GEOS is not converted to a Python exception:
it ends the runtime and the page has to be reloaded. That includes the
exceptions GEOS normally throws and catches itself, such as a buffer retrying
at lower precision. So `overlay.py` never gives GEOS the chance:

- union, difference and intersection run on a fixed-precision grid, the GEOS
  mode that is robust by construction;
- offsets (grow, shrink, stroke outlines) and repairs of invalid shapes are
  done by Clipper (`pyclipper`), the integer library slicers use, not by GEOS
  `buffer` or `make_valid`;
- simplification is a Douglas-Peucker pass written here, not GEOS `simplify`.
  The GEOS topology-preserving simplifier walks its lines in an order that
  depends on memory addresses, so it crashed on one logo only when that logo
  was processed inside a batch.

The grid overlay has one known way to throw: input with detail about as fine
as the grid itself ("side location conflict"). Shapes that have been through
a cleanup radius have none. Shapes stitched together from the scraps of
other set operations do, so they go through `without_specks` (Clipper) before
GEOS sees them again, and questions about such scraps are answered with
point-in-polygon tests, not overlays. Five of 720 real icons threw in
`refine._absorb_enclosed` until it was written that way.

New geometry code must go through `overlay.py` instead of calling `.union()`,
`.difference()`, `.intersection()`, `.buffer()`, `.simplify()` or
`make_valid` on a shape. The same goes for building geometry from numbers
that could be NaN, such as the bounds of an empty shape: GEOS rejects those
with an exception too.

A clean run proves less than it seems here, because one of these failures
was not repeatable. When touching `overlay.py`, run a few hundred real SVGs
through `build_pad` inside Pyodide more than once.

## Logger callback pattern

`build_pad` and the functions in `batch.py` take a callable that receives
each log line; without one they `print`. The desktop GUI passes a callback
that feeds its Console tab, worker processes pass one that writes to a queue,
and the browser passes a JS function that appends to the on-page console.
Each file logs one summary line and then a `NOTE:` line per warning.

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
`/input.svg`, passes the form values via `pyodide.toPy(settings)`, and calls
`build_pad(..., PadSettings.from_params(js_settings))`. `from_params` is the
same constructor the desktop app uses and rejects names it does not know, so
a renamed form field fails loudly. The Python logger callback marshals
strings back to the JS `log()` function for the on-page console.

After processing, `design.preview()` returns the art, holes, centerline and
pyramid positions as plain lists, pulled to JS via
`pyodide.globals.get('preview_data').toJs({ dict_converter: Object.fromEntries })`.
The 2D canvas draws from that data (flipping Y, since the design is Y-up);
the 3D canvas (when STL is enabled) parses the openscad-wasm STL output via
three.js's `STLLoader`.

If the geometry library crashes the runtime, the promise from
`runPythonAsync` never settles. `main.js` listens for the uncaught global
error instead, marks the file failed, and asks for a reload.

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
stomppad/                         geometry package (see the module table above)
tests/                            pytest suite: python -m pytest
bulk_processor_gui.py             tkinter GUI + process pool + STL queue
bulk_processor.spec               PyInstaller spec (used by CI)
.github/workflows/build.yml       cross-platform binary builds + releases
web/
├── index.html                    upload form + console + result cards
├── package.json                  vite + three (Pyodide loaded from CDN)
├── scripts/prepare.js            sync stomppad + download openscad-wasm
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
