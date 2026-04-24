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
| Output Folder | `.scad` + `.svg` (copied) + `.stl` (if rendered) land here |
| Cache Folder | Skeleton + position cache (per-file subfolder), default `.cache` |
| OpenSCAD Executable | Path to `openscad`; only consulted when "Generate STL" is on |
| Generate STL files | Run OpenSCAD headless after `.scad` generation |
| Use cache | Skip skeleton + position recomputation when a valid cache exists |
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
`[filename]`; a pump thread drains it into the Console tab so you see all
calculator output (parsed point counts, scale factor, skeleton size, valid
positions) as it happens.

## Parameters tab

### SVG processing

- **Target Width / Target Height (mm)** — final pad size. The SVG is scaled
  to fit within both constraints (or just width if height = 0), preserving
  aspect ratio.
- **Samples Per Segment** — points sampled along each SVG path segment.
  Higher = more accurate curves, slower parsing.
- **Skeleton Resolution** — pixel size of the rasterization used for the
  medial-axis transform. Lower = denser skeleton, slower.

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

## Console tab

Streams everything from the worker processes (and from the calculator's own
`logger=` callback) with timestamps. Useful for spotting:

- SVGs that produced an unexpectedly small / large skeleton.
- Cache hits vs reprocessing.
- OpenSCAD stderr when STL render fails.

## Cache system

### How it works

For `mydesign.svg` the first run:

1. Parse SVG → polygon, run skeleton + valid positions.
2. Save `cache_data.json` and a copy of the SVG into `.cache/mydesign/`.

Subsequent runs skip steps 1-2 if `Use cache` is on, and immediately generate
a new `.scad` with whatever OpenSCAD output parameters you currently have.

### When to clear cache (delete `.cache/`)

Cache is invalid if you change anything that affects the geometry pipeline
*before* `.scad` generation:

- Target Width / Height
- Samples Per Segment
- Skeleton Resolution
- Pyramid Size / Spacing / Safety Margin
- Include Rotation
- The original SVG itself

OpenSCAD output parameters (thicknesses, heights, `$fn`) are applied at
`.scad` generation time and **don't** require clearing cache.

## Output structure

```
output/
├── design1.scad
├── design1.svg     (copy — OpenSCAD imports it relatively)
├── design1.stl     (if STL rendering was on)
└── ...

.cache/
├── design1/
│   ├── design1.svg
│   └── cache_data.json
└── ...
```

The generated `.scad` contains `svg_file = "design1.svg"; import(svg_file)`,
which is why we copy the SVG next to it.

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

**`MultiPolygon` errors** — fixed in `c09af24`; updates needed if you're on
an older checkout.

## Running the calculator standalone

`pyramid_position_calculator.py` is a usable script on its own — edit the
constants at the bottom of `main()` and run:

```sh
python pyramid_position_calculator.py
```

Outputs `stomp_pad_precalculated.scad` and a `debug_viz.png` showing the
boundary, skeleton, and pyramid placements. Useful for quick experiments
without the GUI in the way.

## See also

- [../README.md](../README.md) — front door
- [architecture.md](architecture.md) — how the code is organized
- [../web/README.md](../web/README.md) — browser version
