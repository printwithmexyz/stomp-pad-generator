# Stomp Pad Generator

Convert SVG outlines into 3D-printable stomp pads with skeleton-packed grip
pyramids. The geometry pipeline (SVG → components → medial-axis skeleton →
hex-packed pyramid placement → OpenSCAD model → STL) lives in the
[`stomppad/`](stomppad/) Python package and is exposed through a desktop GUI
and an in-browser editor.

## Two ways to use it

| Interface | Best for | How |
|---|---|---|
| **Desktop GUI** | Bulk batches (Files & Folders tab) and single-file editing (Edit tab). Real OS performance, STL rendering via local OpenSCAD. | `python bulk_processor_gui.py` |
| **Browser (Pyodide)** | Single SVGs, sharing a link, no install. Includes the same interactive editor for grouping components, picking per-body colors / patterns, and exporting an STL set or `.3mf`. | `cd web && npm install && npm run dev` |

Both frontends import the same [`stomppad/`](stomppad/) package. Desktop
imports it directly; the web build syncs it via a JSON manifest that
Pyodide unpacks into its virtual filesystem at boot. The top-level
`pyramid_position_calculator.py` is a back-compat shim — new code should
import from `stomppad`.

## v2 features at a glance

- **Component-aware SVG parsing** — each subpath / rect / polygon /
  circle / ellipse becomes its own `Component`; even-odd nesting decides
  which loops are holes; near-full-canvas backgrounds auto-drop.
- **`ShapeProject` model** — components grouped into bodies; each body
  has a color, pattern (`skeleton` / `hexagonal` / `rectangular` /
  `triangular`), and optional per-body pyramid-size override. Serializes
  as a sidecar `<svg-stem>.project.json` next to the source SVG.
- **Interactive editor in both frontends** — click components to select,
  shift-click to multi-select, group into bodies, swap pattern / color /
  enabled. Re-pack only the bodies you change (per-body cache).
- **Multicolor export** — STL set (one `.stl` per body + `print-guide.txt`)
  or generic `.3mf` (per-object color via base materials). Loads into any
  modern slicer.

## Quick start — desktop

```sh
pip install -r requirements_bulk_processor.txt
python bulk_processor_gui.py
```

The GUI opens with four tabs: Files & Folders (batch mode), Parameters,
Console, and **Edit** (single-file v2 editor). You'll also need
[OpenSCAD](https://openscad.org/downloads.html) installed for STL
rendering and for the Edit tab's Export buttons. See
[docs/desktop.md](docs/desktop.md) for the full walkthrough.

## Quick start — web

```sh
cd web
npm install
npm run dev
```

Open the printed URL. First load takes ~30–60 s while Pyodide downloads
the scientific Python wheels (cached afterward). The page has the legacy
batch flow in sections 1–3 and the interactive single-file editor in
section 4. See [web/README.md](web/README.md) for deploy instructions
(Vercel-ready) and [docs/architecture.md](docs/architecture.md) for the
cross-frontend design.

## Pre-built binaries

GitHub Actions builds windowed executables for Windows / macOS / Linux on
every push (see `.github/workflows/build.yml`). Download artifacts from
the **Actions** tab on GitHub, or grab a tagged release from
**Releases**. No Python install required for end users — but they still
need OpenSCAD on PATH for STL rendering and Export.

## Repo layout

```
.
├── stomppad/                       # geometry pipeline package
│   ├── geometry.py                 #   component-aware SVG parsing
│   ├── packing.py                  #   skeleton + footprint + pack_component
│   ├── patterns/                   #   skeleton / hexagonal / rect / triangular
│   ├── project.py                  #   ShapeProject + stateful editor API
│   ├── cache.py                    #   per-body output cache
│   ├── openscad.py                 #   SCAD generation (legacy + multi-body)
│   └── exporters/                  #   stl_set + threemf
├── pyramid_position_calculator.py  # back-compat shim → stomppad
├── bulk_processor_gui.py           # desktop tkinter GUI (batch + edit tabs)
├── desktop_editor.py               # tkinter Edit tab implementation
├── bulk_processor.spec             # PyInstaller spec (used by CI)
├── tests/                          # pytest suite (4 files, ~74 tests)
├── web/                            # browser version (Vite + Pyodide + openscad-wasm)
├── docs/                           # extended documentation + v2 plan
└── .github/workflows/build.yml     # cross-platform binary builds + releases
```

## Documentation

- [docs/desktop.md](docs/desktop.md) — full desktop GUI guide (batch + Edit tab)
- [docs/architecture.md](docs/architecture.md) — code map and cross-interface design
- [docs/v2-plan.md](docs/v2-plan.md) — implementation plan for the v2 multicolor / editor work
- [web/README.md](web/README.md) — web subproject + Vercel deploy
- [CHANGELOG.md](CHANGELOG.md) — release notes
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to contribute

## License

GPL v3. See [LICENSE](LICENSE).
