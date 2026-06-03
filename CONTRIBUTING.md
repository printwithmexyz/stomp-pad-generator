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
your change touches the STL render path (batch tab) or the Edit tab's
Export STL set / Export 3MF buttons. Full walkthrough:
[docs/desktop.md](docs/desktop.md).

### Web (Vite + Pyodide + openscad-wasm)

```sh
cd web
npm install
npm run dev
```

`npm install` runs `scripts/prepare.js` which walks the parent
[`stomppad/`](stomppad/) package, writes
`public/stomppad-manifest.json` (the manifest Pyodide unpacks into its
virtual FS at boot), and downloads the openscad-wasm release files
(~8 MB, cached on disk). The web README has deploy notes:
[web/README.md](web/README.md).

### Tests

```sh
pip install -r requirements-dev.txt   # adds pytest + pinned numpy/scikit/shapely
pytest tests/
```

Four test files: `test_regression.py` (Phase 0 byte-equality golden),
`test_phase1.py` (component parser, packing, patterns, ShapeProject),
`test_phase2.py` (stateful editor API + sidecar + cache), `test_phase3.py`
(multi-body SCAD, STL set, 3MF). The byte-equality test depends on pinned
numpy / scikit-image / shapely versions — minor-version drift in any of
those flips the captured `.scad`. If you intentionally bump a pin,
recapture the golden via `python tests/fixtures/_capture_golden.py` and
review the diff before committing.

## Where to make a change

The geometry + editor + export pipeline lives in the
[`stomppad/`](stomppad/) package. Submodule layout (matches
[docs/architecture.md](docs/architecture.md)):

| Module | What lives here |
|---|---|
| `geometry.py` | `parse_svg_to_components`, `Component`, nesting + background drop |
| `packing.py` | skeleton, footprint, `pack_component`, `PackContext` |
| `patterns/` | `PatternStrategy` registry + 4 built-in strategies |
| `project.py` | `Body`, `ShapeProject`, stateful editor API, sidecar I/O |
| `cache.py` | `BodyOutputCache` (per-body re-pack) |
| `openscad.py` | legacy single-shape SCAD + multi-body `generate_body_scad` |
| `exporters/` | `build_stl_set`, `write_stl_set`, `build_threemf` |

Both the desktop GUI (`bulk_processor_gui.py` + `desktop_editor.py`) and
the web frontend (`web/src/editor.js`) import from `stomppad`. The
top-level `pyramid_position_calculator.py` is a back-compat shim — new
code should target `stomppad` directly. The web build re-syncs the
package on every `npm run dev` / `npm run build`, so no manual copying.

When changing a function signature, audit both call sites:

```sh
grep -rn "function_name" stomppad/ desktop_editor.py bulk_processor_gui.py \
     web/src/ tests/
```

For deeper context on how the pieces fit together (logger callbacks,
desktop process pool + STL queue, web Pyodide ↔ JS interop, observer
events, openscad-wasm loader gotcha), read
[docs/architecture.md](docs/architecture.md).

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

## License

This project is GPL v3. Contributions are accepted under the same license —
by opening a PR you confirm your changes can be released under GPL v3. See
[LICENSE](LICENSE) for the full text.
