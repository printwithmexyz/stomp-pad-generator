# Stomp Pad Generator

Convert SVG outlines into 3D-printable stomp pads with skeleton-packed grip
pyramids. The geometry pipeline (SVG → polygon → medial-axis skeleton →
hex-packed pyramid placement → OpenSCAD model → STL) is one Python module,
exposed through two interfaces.

## Two ways to use it

| Interface | Best for | How |
|---|---|---|
| **Desktop GUI** | Bulk batches, real OS performance, STL rendering via local OpenSCAD | `python bulk_processor_gui.py` |
| **Browser (Pyodide)** | Single SVGs, sharing a link, no install | `cd web && npm install && npm run dev` |

Both share the same `pyramid_position_calculator.py`. Desktop runs it under a
`ProcessPoolExecutor`; browser runs it in WebAssembly via Pyodide. STL
rendering is OpenSCAD on the desktop and openscad-wasm in the browser.

## Quick start — desktop

```sh
pip install -r requirements_bulk_processor.txt
python bulk_processor_gui.py
```

You'll also need [OpenSCAD](https://openscad.org/downloads.html) installed if
you want STL rendering (the GUI lets you point at the executable). See
[docs/desktop.md](docs/desktop.md) for the full walkthrough — file/folder
config, parameter reference, cache system, parallel processing model,
preview-debug mode, troubleshooting.

## Quick start — web

```sh
cd web
npm install
npm run dev
```

Open the printed URL. First load takes ~30–60 s while Pyodide downloads the
scientific Python wheels (cached afterward). See
[web/README.md](web/README.md) for deploy instructions (Vercel-ready) and
[docs/architecture.md](docs/architecture.md) for how the in-browser pipeline
hangs together.

## Pre-built binaries

GitHub Actions builds windowed executables for Windows / macOS / Linux on
every push (see `.github/workflows/build.yml`). Download artifacts from the
**Actions** tab on GitHub, or grab a tagged release from **Releases**. No
Python install required for end users — but they still need OpenSCAD on PATH
if they want STL rendering.

## Repo layout

```
.
├── pyramid_position_calculator.py  # geometry pipeline (single source of truth)
├── bulk_processor_gui.py           # desktop tkinter GUI
├── bulk_processor.spec             # PyInstaller spec (used by CI)
├── web/                            # browser version (Vite + Pyodide + openscad-wasm)
├── docs/                           # extended documentation
└── .github/workflows/build.yml     # cross-platform binary builds + releases
```

## Documentation

- [docs/desktop.md](docs/desktop.md) — full desktop GUI guide
- [docs/architecture.md](docs/architecture.md) — code map and cross-interface design
- [web/README.md](web/README.md) — web subproject + Vercel deploy
- [CHANGELOG.md](CHANGELOG.md) — release notes
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to contribute

## License

GPL v3. See [LICENSE](LICENSE).
