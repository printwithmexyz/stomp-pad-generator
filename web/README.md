# Stomp Pad Generator — Web

Browser version of the bulk processor. Runs the same Python pipeline as the
desktop app, but executes entirely client-side via [Pyodide](https://pyodide.org/)
(numpy, scipy, scikit-image, shapely all running in WebAssembly).

No backend. The only network calls are to load Pyodide + packages from the
jsDelivr CDN on first visit.

## Source-of-truth

`pyramid_position_calculator.py` lives in the repository root. The `prepare`
script (run automatically before `dev` and `build`) copies it into
`public/pyramid_position_calculator.py`, which Pyodide fetches and imports at
runtime. Edit the parent file, restart `npm run dev`, and changes propagate.

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
- STL rendering runs in-browser via
  [openscad-wasm](https://github.com/openscad/openscad-wasm). The release
  assets (`openscad.js`, `openscad.wasm`, `openscad.wasm.js`, ~8 MB) are
  downloaded by `scripts/prepare.js` into `public/openscad/` on first install
  and cached on disk from then on. Bump `OPENSCAD_VERSION` in `prepare.js` to
  upgrade.
- Pyodide can't use multiprocessing, so multi-file uploads run sequentially.
  The desktop app's process pool is the way to go for bulk batches.

## See also

- [../README.md](../README.md) — repo front door
- [../docs/architecture.md](../docs/architecture.md) — how the in-browser pipeline + Vite quirks fit together
- [../docs/desktop.md](../docs/desktop.md) — desktop GUI guide
