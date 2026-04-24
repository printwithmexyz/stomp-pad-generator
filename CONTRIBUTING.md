# Contributing

Thanks for considering a contribution! This is a small, single-maintainer
project — the bar for contributions is "fixes a real problem or adds value
the maintainer can support." Drive-by refactors that churn style or
introduce dependencies will likely be declined.

## Filing an issue

- Use **Issues** for bug reports, behavior questions, and feature ideas.
- For bugs, include: OS, Python version (or browser), the SVG that triggered
  it (if you can share it), and the full Console output. Bug reports without
  reproducible input are hard to act on.
- Security-sensitive reports: please don't open a public issue. Reach the
  maintainer through GitHub directly.

## Development setup

Two interfaces live in this repo. Pick the one you're touching.

### Desktop (Python / tkinter)

```sh
pip install -r requirements_bulk_processor.txt
python bulk_processor_gui.py
```

You'll need [OpenSCAD](https://openscad.org/downloads.html) installed if
your change touches the STL render path. Full walkthrough:
[docs/desktop.md](docs/desktop.md).

### Web (Vite + Pyodide + openscad-wasm)

```sh
cd web
npm install
npm run dev
```

`npm install` runs `scripts/prepare.js` which copies the parent's
`pyramid_position_calculator.py` into `public/` and downloads the
openscad-wasm release files (~8 MB, cached on disk). The web README has
deploy notes: [web/README.md](web/README.md).

## Where to make a change

The geometry pipeline is a single Python module:
**`pyramid_position_calculator.py`**. Both the desktop GUI and the browser
import it directly — there is no parallel implementation to keep in sync.
If you fix a bug or change behavior here, both interfaces benefit
immediately. The web project re-syncs the file on every `npm run dev` /
`npm run build`, so no manual copying.

When changing a function signature, audit both call sites:

```sh
grep -n "function_name" bulk_processor_gui.py web/src/main.js
```

For deeper context on how the pieces fit together (logger callbacks,
desktop process pool + STL queue, web Pyodide ↔ JS interop, openscad-wasm
loader gotcha), read [docs/architecture.md](docs/architecture.md).

## Branches and PRs

- Branch from `main` with a descriptive name: `fix/<short>` or
  `feature/<short>`.
- Keep PRs focused — one logical change per PR. Bundled refactors are hard
  to review.
- Don't push to `main` directly.
- The CI workflow (`.github/workflows/build.yml`) runs on every push to
  `feature/**`, so you'll see build artifacts for Windows / macOS / Linux
  on the **Actions** tab. A green build is a soft prerequisite for merge.

## Style

- Match the surrounding style. The Python code is plain (no formatter is
  enforced). The JS is vanilla ES modules — no framework, no TypeScript.
- Default to no comments. Add one only when the *why* is non-obvious
  (workaround, hidden constraint, surprising behavior). Don't restate what
  the code does.
- Don't add dependencies without flagging them in the PR description.
  Specifically for the web subproject: anything bundled (in `dependencies`)
  ships to every visitor, so think hard before adding.

## Testing

There's no formal test suite (yet). For now:

- For Python changes, run the desktop GUI against a small folder of SVGs
  and compare the generated `.scad` and `.stl` against a known-good run.
- For web changes, exercise the upload → process → preview → download flow
  in `npm run dev`. Try multi-file uploads and an SVG that produces a
  `MultiPolygon` (logo with multiple disjoint shapes) to catch regressions
  in the geometry pipeline.

If you add a test suite, prefer `pytest` for Python and Vitest for the web.

## License

This project is GPL v3. Contributions are accepted under the same license —
by opening a PR you confirm your changes can be released under GPL v3. See
[LICENSE](LICENSE) for the full text.
