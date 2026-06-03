# Stomp Pad Generator — Web

Browser version of the bulk processor + interactive single-file editor.
Runs the same Python pipeline as the desktop app, executed entirely
client-side via [Pyodide](https://pyodide.org/) (numpy, scipy,
scikit-image, shapely all running in WebAssembly).

No backend. The only network calls are to load Pyodide + packages from the
jsDelivr CDN on first visit.

## Source-of-truth

The [`stomppad/`](../stomppad/) Python package in the repository root is
the single source of truth for geometry, packing, pattern strategies,
project model, cache, OpenSCAD generation, and exporters. The `prepare`
script (run automatically before `dev` and `build`) walks the package,
writes a `public/stomppad-manifest.json` containing every `.py` source
inlined, and `web/src/main.js` unpacks the manifest into Pyodide's
virtual filesystem at boot. Edit any file under `stomppad/`, restart
`npm run dev`, and the change propagates.

The v2 plan originally called for shipping the package as a `.zip` —
the manifest approach lands the same multi-file sync guarantee without a
new Node dependency (Node has no built-in zip writer). See
[`docs/architecture.md`](../docs/architecture.md) for the rationale.

## Page sections

- **1–3** — legacy batch flow. Upload SVGs, set parameters, click
  Process all. Each result card shows a 2D preview + downloadable
  `.scad` / `.svg` / `.stl`.
- **4** — interactive single-file editor (v2). Pick one SVG; the canvas
  renders each component colored by its body, click to select (shift to
  multi-select), the right sidebar gives you per-body color swatch /
  enable checkbox / pattern dropdown / size override. Buttons:
  - **Download .project.json** — exports the editor state as a sidecar
    JSON the desktop / future you can re-load.
  - **Export STL set (.zip)** — renders each enabled body via
    openscad-wasm and downloads a zip with one `.stl` per body plus a
    `print-guide.txt`.
  - **Export .3mf** — same renders, packed into a single generic 3MF
    with per-object color.
  - **Auto-prepare** toggle — the browser can't write next to the
    source SVG, so "auto-save" keeps the JSON encoding fresh on every
    edit and shows a "prepared at" stamp; you still click Download to
    save the file.

## Local development

```sh
cd web
npm install
npm run dev
```

Open the printed URL (usually http://localhost:5173). First load takes
30–60 seconds while Pyodide downloads scientific Python packages
(~50 MB total). Browser cache makes subsequent loads instant.

## Build

```sh
npm run build
```

Output goes to `dist/`. Preview the production build with `npm run preview`.

## Deploy to Vercel

In Vercel project settings:

- **Root Directory**: `web`
- **Framework Preset**: Vite (auto-detected)
- **Build Command**: `npm run build` (default)
- **Output Directory**: `dist` (default)

That's it. Vercel runs `npm install` (which triggers `prepare` via the
`prepare` lifecycle script) and then `npm run build`.

## Notes

- All processing is client-side; no SVG ever leaves the user's browser.
- STL rendering runs in-browser via openscad-wasm. We pull the official
  OpenSCAD playground build (`files.openscad.org/playground/...`, manifold
  backend, ~8 MB) instead of the GitHub releases (last tagged 2022,
  CGAL-only — that build asserts on geometry the manifold backend handles
  cleanly). `scripts/prepare.js` downloads + unzips into `public/openscad/`
  on first install, writes a VERSION file, and skips the work on subsequent
  installs. Bump `OPENSCAD_VERSION` in `prepare.js` to upgrade.
- Pyodide can't use multiprocessing, so multi-file uploads run sequentially.
  The desktop app's process pool is the way to go for bulk batches.
- The editor's STL set + 3MF exports invoke openscad-wasm sequentially —
  one body at a time, sharing a single WASM instance. For a five-body
  project on a typical machine that's a few seconds per body. The page
  remains responsive during the run (each render returns to the event
  loop).

## See also

- [../README.md](../README.md) — repo front door
- [../docs/architecture.md](../docs/architecture.md) — how the in-browser pipeline + Vite quirks fit together
- [../docs/desktop.md](../docs/desktop.md) — desktop GUI guide (batch + Edit tab)
- [../docs/v2-plan.md](../docs/v2-plan.md) — v2 implementation plan
