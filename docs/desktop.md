# Desktop GUI Guide

The tkinter app in `bulk_processor_gui.py` is the production interface for
bulk batches. It runs the geometry pipeline in worker processes (true
parallelism via `ProcessPoolExecutor`) and renders STLs through a separate
thread pool that overlaps with SVG processing on the next files.

## Requirements

- **Python 3.10+** (3.12 is what CI builds against).
- **Python deps** (one command):
  ```sh
  pip install -r requirements_bulk_processor.txt
  ```
  Pulls `shapely`, `numpy`, `scipy`, `scikit-image`, `svg.path`, `matplotlib`.
- **OpenSCAD** for STL rendering — only needed if you tick "Generate STL files".
  Download from <https://openscad.org/downloads.html>. The GUI lets you point
  at the executable; on Windows the default path is
  `C:\Program Files\OpenSCAD\openscad.exe`.

## Launch

```sh
python bulk_processor_gui.py
```

Pre-built binaries (no Python install needed) are published as artifacts on
every CI run and as zips on tagged releases — see the **Actions** and
**Releases** tabs on GitHub. The CI workflow lives at
`.github/workflows/build.yml` and produces Windows `.exe`, macOS `.app`, and
Linux binary in one matrix job.

## Files & Folders tab

| Field | Purpose |
|---|---|
| Input Folder | Source directory; every `*.svg` inside is processed |
| Output Folder | `.scad` + `.stl` (if rendered) land here |
| Cache Folder | Finished-design cache (per-file subfolder), default `.cache` |
| OpenSCAD Executable | Path to `openscad`; only consulted when "Generate STL" is on |
| Generate STL files | Run OpenSCAD headless after `.scad` generation |
| Use cache | Reuse a file's cached design when neither the SVG nor any setting changed |
| Preview debug visualization | Render a matplotlib PNG and pause for approval per file |
| Parallel Threads | Worker process count for SVG + thread count for STL render |

### Parallel Threads — what the number actually controls

When `Threads ≥ 2` and "Preview debug" is **off**, the app runs:

- A `ProcessPoolExecutor` with N worker processes for SVG → `.scad`.
- A `ThreadPoolExecutor` with N threads for OpenSCAD STL rendering.

The two pools run concurrently — STL render of `file_3.scad` happens while
worker processes are still chewing on `file_5.svg` and `file_6.svg`. With
`Threads = 1` or "Preview debug" on, the app falls back to a sequential
in-process loop (the preview dialog can't cross process boundaries).

Worker processes write to a `multiprocessing.Manager` queue prefixed with
`[filename]`; a pump thread drains it into the Console tab so you see each
file's summary (regions, colors, final size, pyramid count) and its notes as
they happen.

## Parameters tab

### SVG processing

- **Target Width / Target Height (mm)** — final pad size. The SVG is scaled
  to fit within both constraints (or just width if height = 0), preserving
  aspect ratio.
- **Samples Per Segment** — points sampled along each SVG path segment.
  Higher = more accurate curves, slower parsing.
- **Skeleton Resolution** — pixel size of the rasterization used for the
  medial-axis transform. Lower = denser skeleton, slower.
- **Keep a full-canvas background rectangle** — off by default. A plain
  rectangle that blankets the canvas underneath other artwork is treated as
  a background and dropped, and anything in its color becomes a cutout. Tick
  this when that rectangle is really the pad's shape.

The SVG is read the way a browser paints it: transforms, CSS classes, fill
rules, strokes and opacity are honored, and later shapes cover earlier ones.
Slivers and specks too small to print are removed. The Console lists what
was dropped or trimmed.

### Pyramid packing

- **Pyramid Size** — base width of each grip pyramid (mm).
- **Pyramid Spacing** — gap between adjacent pyramids in the hex grid.
- **Safety Margin** — inset from the boundary so pyramids don't overhang.
- **Include Rotation** — align pyramids with the local skeleton tangent
  (recommended for elongated shapes).

### OpenSCAD output

- **Base Thickness** — flat base layer thickness.
- **Outline Offset** — width of the raised border around the pad.
- **Outline Height** — height of the raised border.
- **Pyramid Height** — pyramid peak height above the base.
- **Pyramid Style ($fn)** — sides of each pyramid; 4 = square pyramid,
  3 = triangular pyramid, 8 = octagonal cone-like.

### Holes in artwork

A hole is any area fully enclosed by the artwork: the inside of a ring, the
counter of a letter.

- **Cut through the pad** — the hole goes all the way through, with the rim
  running around its edge. Anything inside the hole prints as a separate
  loose piece. A hole narrower than twice the Outline Offset fills in with
  rim, and the Console says so.
- **Recess, keeping a floor** — the hole keeps a floor **Recess Depth** mm
  below the surface the pyramids stand on, so everything stays one piece. A
  depth of the Base Thickness or more cuts through instead.

Both values are also written at the top of the `.scad` as `recess_voids`
and `recess_depth`, so you can flip them in OpenSCAD without regenerating.

## Console tab

Streams everything from the worker processes with timestamps. Useful for
spotting:

- `NOTE:` lines: dropped backgrounds, trimmed detail, regions too narrow for
  pyramids, holes the rim fills in, and how many separate pieces the pad
  prints as.
- Cache hits vs reprocessing.
- OpenSCAD stderr when STL render fails.

## Cache system

For `mydesign.svg` the first run builds the design and saves it as
`.cache/mydesign/design.json`. A later run with `Use cache` on reuses it only
if the SVG file and every setting are unchanged; the cache is keyed on both,
so it never goes stale and there is nothing to clear by hand. Change any
parameter and the file is simply rebuilt.

`cache_data.json` files from older versions are ignored and safe to delete.

## Output structure

```
output/
├── design1.scad
├── design1.stl          (if STL rendering was on)
├── design1_debug.png    (if Preview debug was on)
└── ...

.cache/
├── design1/
│   └── design.json
└── ...
```

The `.scad` carries the artwork as inline polygons, so it stands alone. It no
longer imports the SVG and the SVG is no longer copied next to it.

## Troubleshooting

**"OpenSCAD executable not found"** — fix the path in Files & Folders or
install OpenSCAD.

**"STL rendering failed"** — open the generated `.scad` in OpenSCAD by hand;
it'll show whatever syntax/geometry error happened. Also check the Console
for OpenSCAD's stderr.

**Workers seem stuck for ~10 s after clicking Start** — first run cost on
Windows; each spawned worker re-imports `numpy`/`scipy`/`scikit-image`. Once
they're warm, throughput is steady.

**Preview-debug mode runs sequentially even with Threads = 4** — by design.
The preview dialog needs the GUI's `self`, which can't be pickled across
processes.

**"No filled shapes found" / "Text is ignored"** — the SVG has live text or
only an embedded image. Convert text to outlines, or trace the image to
vector shapes, then export again.

**The pad came out as a plain rectangle, or the logo is missing** — check the
Console for a background note and try the "Keep a full-canvas background
rectangle" box either way.

## Running the pipeline without the GUI

```python
from stomppad.batch import process_svg
from stomppad.pad import PadSettings

settings = PadSettings(target_width=152, void_mode='recess', recess_depth=0.6)
process_svg('logo.svg', 'output', settings)   # writes output/logo.scad
```

`stomppad.batch.save_debug_visualization(design, 'debug.png')` draws the
artwork, holes, centerline and pyramid bases for a design from
`stomppad.pad.build_pad`.

## See also

- [../README.md](../README.md) — front door
- [architecture.md](architecture.md) — how the code is organized
- [../web/README.md](../web/README.md) — browser version
