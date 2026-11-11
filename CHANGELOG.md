# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

When you tag a release with `v*`, the [build workflow](.github/workflows/build.yml)
publishes Windows / macOS / Linux binaries automatically. Copy the relevant
section below into the GitHub release body.

## [Unreleased]

### Added

- **`stomppad/` package** replaces `pyramid_position_calculator.py` as the
  shared geometry pipeline, with a pytest suite (`python -m pytest`).
- **Hole toggle.** Holes enclosed by the artwork either cut through the pad
  or are recessed to a set depth below the pyramid floor. In the desktop
  Parameters tab, the web form, and at the top of each `.scad` as
  `recess_voids` / `recess_depth`.
- **Notes in the console** for dropped backgrounds, trimmed detail, regions
  too narrow for pyramids, holes the rim fills in, and how many separate
  pieces the pad prints as.
- **Keep background** option for SVGs whose full-canvas rectangle is the pad
  itself.
- `pyclipper` is a new dependency (desktop `requirements_bulk_processor.txt`;
  the web app loads it from Pyodide).
- **Multicolor bodies and 3MF export in the `stomppad` package** (not in the
  apps yet). A pad splits into a base plus one body per other color, each
  owning the top `color_depth` of the floor and its pyramids. The `.scad`
  renders any one body with `-D part=N`, and `stomppad.threemf` packs the
  body STLs into a 3MF whose parts carry Bambu Studio filament slots.

### Changed

- **SVGs are read the way a browser paints them**: nested transforms, CSS
  classes and inline styles, fill rules, strokes, opacity, paint order, unit
  and percent lengths. Full-canvas background rectangles are dropped, and
  slivers too small to print are removed.
- **The `.scad` carries its geometry inline** and no longer imports the SVG.
  The SVG is no longer copied to the output folder, and the web app no longer
  offers it as a download.
- **Desktop cache** is keyed on the SVG and every setting. Old
  `cache_data.json` files are ignored.
- **Cleanup no longer leaves gaps between colors.** Detail too small to
  print that other colors surround is absorbed by a neighbour.
- **STL renders fail loudly** when OpenSCAD reports leaving a shape out,
  instead of saving an STL with material missing.
- Pyramid bases are checked in the shape the `.scad` draws them, so
  `Pyramid Style` values other than 4 fit properly.

### Fixed

- Holes in the artwork were filled with pyramids.
- `transform` attributes were ignored, so pyramids and outline could disagree.
- A background rectangle swallowed the logo.
- Separate shapes were stitched into one outline.
- SVGs without an `xmlns`, or with a comma-separated `viewBox`, failed.
- Pyramid rotation was mirrored on diagonal strokes.
- The same SVG could produce a different pyramid count from run to run.
- The desktop cache returned stale results after a parameter change.
- Shapes that meet at a single point (two squares corner to corner) could
  make OpenSCAD drop the cavity cut and render a solid slab.
- The web app could lose its Python runtime on roughly 3 in 100 logos and
  hang. Geometry operations can no longer trigger that, and the page reports
  it if it ever happens.

### Removed

- `pyramid_position_calculator.py` and `requirements_pyramid_calc.txt`.

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
