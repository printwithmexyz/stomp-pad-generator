# Stomp Pad Generator v2 — Implementation Plan

**Status:** Plan only. No code until approved. Per `CLAUDE.md`, each phase is a
separate review gate (max 5 files/response, `code-review-tester` after each
logical chunk, `readme-auditor` after doc-affecting changes).

## Revision 2026-11-11 (supersedes the sections below where they conflict)

Scope for this round is correct SVG handling, multicolor, and 3MF. Decisions
changed in the second interview:

| Topic | Was | Now |
|---|---|---|
| Interactive editor (Phase 2) | In scope, web + desktop | **Deferred.** Manual tweaks are a region list (drop / recolor by id), not a canvas editor. |
| Who reads the SVG | Python for packing, OpenSCAD `import()` for the body | **Python only.** Shapes are written into the `.scad` as polygons. Two parsers disagreeing caused the transform and Y-flip bugs, and `import()` cannot drop backgrounds or split colors. |
| Shapes inside shapes | Even-odd nesting across elements decides hole vs body | **Paint order.** Regions are what a browser would show: later elements cover earlier ones, fill rules decide holes within a path. Anything painted in the dropped background's color, or in a listed void color, cuts a hole. |
| Color layout | Base is a colorable body | **Shared base, colored top.** One color for the bottom layers and rim; each color region owns its top layers and pyramids. |
| 3MF | Generic only | **Generic plus Bambu part-to-filament metadata**, format to be verified against a real Bambu Studio export. |
| Holes in the artwork | Always cut through | **Toggle.** `void_mode='through'` cuts enclosed holes through the pad, rim and all. `void_mode='recess'` keeps a floor `recess_depth` below the surface the pyramids stand on, so islands inside a hole stay attached. A recess as deep as the base falls back to through. |

Phases, each a review gate:

- **A. Regions (done, not wired in).** `stomppad/svg_parse.py` and
  `stomppad/refine.py`: transforms, CSS classes, fill rules, strokes, paint
  order, background drop, void fills, scaling to mm, cleanup (near-duplicate
  colors, slivers, specks, pinholes), printability warnings, drop / recolor
  overrides. Tests in `tests/test_svg_regions.py` (`python -m pytest`).
- **B1. Pad build (done, not wired in).** `stomppad/pad.py` is the one entry
  point: `build_pad(svg, PadSettings)` returns a `PadDesign`, and
  `stomppad/openscad.py` writes it as a `.scad` with inline polygons and no
  SVG import. `stomppad/packing.py` places pyramids per region on one shared
  hex grid. Tests in `tests/test_pad_build.py`.
- **B2. Desktop (done).** The bulk processor calls `stomppad.batch`, the
  Parameters tab has the hole toggle and recess depth, and the cache is keyed
  on the SVG and every setting.
- **B3. Web (done).** `prepare.js` copies the package into
  `public/stomppad/` with a manifest, `main.js` calls `build_pad`, the page
  has the same controls, and the 2D preview draws holes. Checked end to end
  in Chrome. `pyramid_position_calculator.py` is deleted. STL rendering in the
  browser has not been exercised since the change.
- **C. Multicolor export.** One body per color plus the base, STL set, 3MF.
- **D. Region list UI.** Show regions, warnings, and drop / recolor controls
  in web and desktop.

Fixed on the way through B1, both present in the old
`pyramid_position_calculator.py`:

- Pyramid rotation was mirrored. Angles were measured with Y down and applied
  with Y up, so on a diagonal stroke the pyramids turned the wrong way and
  could overhang a thin one. Coordinates are now Y up from the start.
- The skeleton step breaks ties at random, so the same SVG could give a
  different pyramid count from run to run. It is now seeded.

Known gaps:

- **GEOS errors kill Pyodide (handled).** In Pyodide (checked on 0.26.4 and
  0.29.0) a C++ exception from GEOS is not turned into a Python exception. It
  ends the runtime and the page needs a reload. With GEOS buffers, 3 of 91
  real icons crashed. `stomppad/overlay.py` now runs set operations on a
  fixed-precision grid and offsets with Clipper (`pyclipper`), so GEOS is
  never asked to do anything it can fail at. Measured in Pyodide on 354 real
  icons, both hole modes: 0 crashes. The page still detects a crash and asks
  for a reload if one ever happens. Nothing proves no input can trigger one,
  so new geometry code has to keep to the rule in `docs/architecture.md`.
- The suite passes inside Pyodide 0.26.4 (shapely 2.0.2, scikit-image 0.23.2)
  apart from the desktop-only batch tests.
- SVGs with DOCTYPE entity declarations (Illustrator "preserve editing
  capabilities") are rejected with a clear error on both desktop and web.
  Re-export as plain SVG.
- A bevelled stroke corner comes out slightly fuller than a browser draws it,
  because Clipper has no true bevel join.
- Ignored without a warning: CSS `transform` in a `style` attribute, nested
  `<svg>` viewports, `stroke-dasharray`, and the `color` property behind
  `currentColor`.
- Cleanup removes a sliver but leaves its footprint empty, so a 0.3 mm stripe
  between two colors becomes a 0.3 mm groove. Decide in Phase C whether the
  neighbour should absorb it.
- A full-canvas plain rectangle under other art is always read as background.
  If it is really the pad's base shape, pass `keep_background`.

## Decisions locked (from interview)

| Decision | Choice |
|---|---|
| Interactive GUI platform | Both web **and** desktop (tkinter) |
| Selection / body / color state | Lives in the **shared Python package** as a serializable `ShapeProject` (JSON). Both frontends are thin renderers. |
| Multicolor export | **STL-set** (one STL per body) **and** a **generic 3MF** (per-object meshes + color; *not* Bambu-specific — 3MF is the fallback) |
| Inner loops (shapes-in-shapes) | Default: **holes are empty** (even-odd nesting). Phase 2 adds a per-loop "hole ↔ body" toggle. |
| Background paths | **Auto-drop** any path covering ~95%+ of the viewBox; editor can re-add. Keeps pre-filled SVGs zero-click. |
| Packing patterns | Per-body strategy: **skeleton-following, hexagonal, rectangular, triangular**. Pattern system is a registry; radial/concentric deferred (need anchor-point UI). |
| Base/outline body | The base **is** a selectable, colorable body (e.g. black base + colored top). |
| Per-body pyramid overrides | Yes — size/height per body, exposed in the editor. |
| SVG fill colors | **Imported as body color defaults** (drives zero-click automated pads). |
| Python→WASM tooling | **No custom wrapper.** Pyodide already is the wrapper. Solve multi-file with a package + zip-sync (Phase 0). `micropip` remains the path to published wheels if ever wanted. |
| Delivery | **Phased.** Phase 0 + 1 ship value on their own. |

---

## Evidence: the real SVG breaks today's pipeline three ways

Test input: `icm_fullxfull...svg` (a melting smiley). Six `<path>`s:

| Path | fill | role | raw area |
|---|---|---|---|
| 1 | `#ffffff` | full-canvas background rectangle | 835,989 |
| 2 | `#010102` | melting-face outline (the ring) | 323,116 |
| 3 | `#ffffff` | white fill inside the ring → **a hole** | 276,612 |
| 4 | `#010102` | eye | 11,221 |
| 5 | `#010102` | eye | 10,828 |
| 6 | `#010102` | mouth | 19,819 |

Running today's `Polygon(all_points).buffer(0)` collapses all 6,120 sampled
points into **one polygon of area 835,989 — the background rectangle.** The
face, the hole, the eyes, and the mouth are all swallowed. **This file is
unusable today**, and it proves every v2 foundation item:

- **Background detection** — path 1 must be auto-dropped, or every automated
  file starts by packing a plain square.
- **Holes ≠ color** — path 3 is the *same color* as the background but is a
  **hole** (it's nested inside the black ring). Nesting decides role, not color.
- **Disjoint bodies** — paths 4/5/6 are three separate islands that should be
  independently selectable/colorable (e.g. eyes one color, mouth another). This
  is the "one body, multiple disjoint volumes" case, from a real file.
- **Mixed patterns in one file** — melt *drips* want skeleton-following; the
  round eyes/face want hexagonal. Per-body pattern strategy is required, not
  cosmetic.

---

## Target architecture (end state)

```
SVG ─► parse_svg_to_components()          # list[Component]; holes preserved;
        │   even-odd nesting; background auto-dropped; fill color captured
        ▼
     ShapeProject  (shared model, JSON-serializable)
        │   components[], bodies[] (component ids + color + enabled +
        │   pattern strategy + pyramid overrides), global params
        ▼
   pack_body(body) ─► PatternStrategy.generate_positions(body)   # registry:
        │              skeleton | hexagonal | rectangular | triangular
        │   holes subtracted; footprints validated via prepared geometry
        ▼
   Renderers (thin):
     • generate_openscad(project)   → multi-body .scad / .stl
     • generate_stl_set(project)    → one .stl per enabled body + print-guide   (P3)
     • generate_3mf(project)        → single generic .3mf w/ per-object color    (P3)
        ▲
        │  ShapeProject JSON in / edits out  (the cross-frontend contract)
   ┌────┴─────────────┐         ┌────────────────────────┐
   │ web/ canvas       │         │ tkinter canvas          │
   │ select/group/     │         │ select/group/           │
   │ color/toggle/     │         │ color/toggle/           │
   │ pattern picker    │         │ pattern picker          │
   └───────────────────┘         └────────────────────────┘
```

`ShapeProject` JSON **is the contract**. A UI click sends "toggle component 3"
or "assign [4,5] to body Eyes, pattern=hexagonal"; Python mutates and returns
updated geometry + preview. Neither frontend owns geometry or selection logic.

---

## Phase 0 — Package the shared Python + zip-sync (prerequisite)

**Why first:** v2's shared code grows from one file to several. Pyodide can't
`import` a multi-file package over single-file fetches, and today's
`prepare.js` copies only `pyramid_position_calculator.py` — so the browser would
silently drift the moment a second module is added. This retires that bug and
unblocks every later phase. Small and mechanical.

- Convert the shared code into a package directory `stomppad/`
  (`__init__.py` re-exporting the current public API so existing imports and the
  standalone `main()` keep working).
- `web/scripts/prepare.js`: zip `stomppad/` → `public/stomppad.zip`; `main.js`
  unpacks it into Pyodide's FS and imports the package. (~15 lines; no new dep.)
- Desktop import path updated; `bulk_processor.spec` datas updated so frozen
  binaries include the package.
- No behavior change. Regression check: existing single-shape output identical.

**Files (≤5):** `stomppad/__init__.py` (new, mostly moved code),
`web/scripts/prepare.js`, `web/src/main.js`, `bulk_processor.spec`,
`docs/architecture.md`.

---

## Phase 1 — Harden packing + multi-loop foundation + pattern registry

**Goal:** Represent the SVG as components-with-holes; auto-drop backgrounds;
capture fill colors; pack each body robustly via a pluggable pattern strategy.
Delivers correct batch output on real files (the smiley) with no UI work.

### 1.1 Component parsing (`stomppad/geometry.py`)
- `parse_svg_to_components()` → `list[Component]`, each a valid `Polygon`
  **with interior rings preserved**, built via `unary_union` + `polygonize`
  (even-odd nesting resolved) instead of one `Polygon(all_points)`.
- **Background auto-drop:** exclude any ring covering ≥95% of the viewBox area
  (configurable threshold; recorded so the editor can re-add).
- **Capture `fill`** per component for later color defaults.
- Keep `parse_svg_to_polygon` as a shim (returns `unary_union(components)`) so
  nothing breaks in one commit.

### 1.2 Hardened packer (`stomppad/packing.py`)
- Pack against `component.difference(holes)` — pyramids never land in a hole.
- Replace per-point `polygon_safe.contains(footprint)` with
  `shapely.prepared.prep(...)` batch tests — speedup **and** the correctness fix
  for thin necks (the melt drips).
- Per-component skeleton (medial axis of that component only).

### 1.3 Pattern registry (`stomppad/patterns/`)
- `PatternStrategy` protocol: `generate_positions(body, solid_geom) -> [(x,y,rot)]`.
- Implement `skeleton.py` (existing logic, refactored), `hexagonal.py`,
  `rectangular.py`, `triangular.py`. All feed the same validation step.
- Registry by name so the model just stores `pattern="hexagonal"`; radial /
  concentric can register later with no caller changes.

### 1.4 ShapeProject model (`stomppad/project.py`)
- `Component(id, exterior, holes, bbox, area, source_fill)`,
  `Body(id, name, component_ids, color_hex, enabled, pattern, pyramid_overrides)`,
  `ShapeProject(components, bodies, global_params, svg_info)`.
- `to_json()/from_json()` (cross-frontend contract). Default grouping: each
  top-level component → own body; holes attach to parent; **body color
  defaults to imported `source_fill`**; default pattern = skeleton.

### 1.5 Tests (`tests/`, new pytest harness)
- Fixtures incl. the **real smiley** + synthetic donut/nested/disjoint/2-hole.
- Assert: background dropped; component & hole counts; **0 pyramid centers in
  any hole**; each pattern fills a known rectangle with expected count; JSON
  round-trip; single-shape regression matches pre-v2 output.

**Files:** split across responses (parsing+model, then packing+patterns, then
tests) to respect the 5-file cap. Gate after each.

---

## Phase 2 — Interactive editor (both frontends)

**Goal:** Click subshapes; group into bodies (incl. disjoint); toggle bodies for
pyramid generation; set per-body pattern + color + pyramid overrides; flip inner
loops hole↔body. All edits flow through the `ShapeProject` API.

### 2.1 Shared selection/edit API (`stomppad/project.py`)
- `hit_test(x,y)->component_id` (holes excluded), `assign_to_body`,
  `toggle_body_enabled`, `set_loop_role(id,"hole"|"body")`, `set_pattern`,
  `set_body_color`, `set_pyramid_overrides`. Geometry-only; no UI imports.

### 2.2 Web editor (`web/src/editor.js` new + `preview-2d.js`)
- Promote read-only canvas to interactive: click/shift-click select, "Group into
  body," per-body color swatch + enable checkbox + pattern dropdown + override
  fields, hole/body toggle on inner loops. Re-pack only edited bodies.

### 2.3 Desktop editor (`bulk_processor_gui.py` "Edit" tab)
- tkinter `Canvas` renders the same components; click coords forwarded to Python
  `hit_test` (no geometry math in tkinter). Same controls. Per-file project
  saved as a sidecar JSON next to the SVG (drives batch reuse).

**Files:** web set, then desktop set (separate responses). Gate after each.

---

## Phase 3 — Multicolor export (STL-set + generic 3MF)

### 3.1 Multi-body OpenSCAD (`stomppad/openscad.py`)
- One named module per enabled body; holes subtracted; base its own body/color.
- Preserve the documented wasm-CGAL coplanar-face workarounds.

### 3.2 STL-set exporter (`stomppad/exporters/stl_set.py`)
- One STL per enabled body (desktop: OpenSCAD `-D`; web: openscad-wasm per
  body) + `print-guide.txt` mapping body → color.

### 3.3 Generic 3MF exporter (`stomppad/exporters/threemf.py`)
- Single `.3mf` (zip: `3dmodel.model` + content types/rels) with one `<object>`
  per body and a base material/color per object — standard 3MF, imports into any
  modern slicer. No Bambu-specific config.
- Automated test: zip parses, N objects, N colors, valid against 3MF core schema
  shape.

### 3.4 UI wiring
- Web + desktop: "Export → STL set / 3MF" using each body's assigned color.

**Files:** exporters, then web wiring, then desktop wiring (separate responses).

---

## Cross-cutting

- **Backward compatibility:** single-shape SVG → one component → one body →
  current output. Regression fixture locks this from Phase 0 on.
- **Web/desktop parity:** the whole `stomppad/` package syncs via the zip step
  (Phase 0), so browser and desktop never drift — including new patterns/exporters.
- **Performance:** prepared-geometry test speeds up the hot loop; per-body
  re-pack avoids recomputing untouched bodies on edits.
- **Dependencies:** none new expected through Phase 3 (3MF is hand-authored
  zip+XML). Any addition flagged first per CLAUDE.md.
- **Docs/CHANGELOG:** updated in whichever phase changes behavior.

## Resolved since first draft
- Base is a colorable body ✓ · per-body pyramid overrides ✓ · import SVG fill
  colors as defaults ✓ · 3MF generic (not Bambu) ✓ · pattern set = skeleton +
  hex + rect + triangular (radial/concentric deferred) ✓ · background auto-drop ✓
  · no custom WASM wrapper; package + zip-sync instead ✓
- **Mixed-fill grouping:** a body's default color = the fill of its
  **largest-area** component. Same-fill groups are unambiguous; always
  click-overridable. ✓
- **Pyramid override granularity: per body** (a body's smaller shapes can run
  far lower density than a large one). Not per-component within a body. ✓
- **Sidecar project JSON: auto-loaded** in batch mode by filename convention
  (`<svg-stem>.project.json` next to the SVG); regenerated if missing. ✓

## Still open
None blocking. Build proceeds Phase 0 → 1 → 2 → 3 on a feature branch, each a
review gate per `CLAUDE.md`.
