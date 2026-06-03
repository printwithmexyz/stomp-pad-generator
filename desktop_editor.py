"""Tkinter implementation of the Phase 2.3 single-file editor.

Lives in its own module so ``bulk_processor_gui.py`` keeps its existing
shape (the batch flow is ~1k lines already; mixing the editor in would
push it over the line and obscure the diff).

Design contract (per v2-plan Phase 2):
- All geometry / packing / mutation logic stays in :mod:`stomppad` —
  this module is a thin tkinter renderer that forwards clicks to
  :meth:`stomppad.ShapeProject.hit_test` and dispatches mutations through
  the same model the web editor uses. No geometry math in tkinter.
- Selection lives on the project (the Phase 2 interview answer);
  re-pack runs through :class:`stomppad.BodyOutputCache` so unchanged
  bodies don't pay the skeleton cost.
- Sidecar save uses the ``<svg-stem>.project.json`` convention. The
  "Auto-save" checkbox is on by default (per the Phase 2 interview);
  toggling it off disables the debounced save loop.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk
from typing import Optional

from stomppad import (
    BodyOutputCache,
    EVENT_BODY_CHANGED,
    EVENT_TOPOLOGY_CHANGED,
    ShapeProject,
    parse_svg_to_components,
    patterns,
)


AUTOSAVE_DELAY_MS = 500
CANVAS_WIDTH = 520
CANVAS_HEIGHT = 520
SIDEBAR_WIDTH = 320
DEFAULT_COLOR = "#888888"


class EditTab:
    """Owns the Edit tab's UI + the editing model for one open SVG."""

    def __init__(self, parent: ttk.Frame, log_callback=None) -> None:
        self._parent = parent
        self._log = log_callback or (lambda _msg: None)

        self._svg_path: Optional[Path] = None
        self._project: Optional[ShapeProject] = None
        self._cache: Optional[BodyOutputCache] = None

        # Canvas transform state (set after each render).
        self._scale: float = 1.0
        self._offset: tuple[float, float] = (0.0, 0.0)
        self._origin: tuple[float, float] = (0.0, 0.0)
        self._canvas_components: dict[int, int] = {}  # component_id -> canvas item id

        # Pending autosave timer handle.
        self._autosave_after_id: Optional[str] = None

        self._build_ui()

    # ----------------------------------------------------------------- #
    # UI scaffolding
    # ----------------------------------------------------------------- #

    def _build_ui(self) -> None:
        # Top: file controls + autosave toggle + status stamp.
        top = ttk.Frame(self._parent)
        top.pack(fill="x", padx=8, pady=(8, 4))

        ttk.Button(top, text="Open SVG…", command=self._on_open_svg).pack(side="left")
        ttk.Button(top, text="Save sidecar now", command=self._save_now).pack(
            side="left", padx=(6, 0)
        )

        self._autosave_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            top,
            text="Auto-save sidecar",
            variable=self._autosave_var,
            command=self._on_autosave_toggle,
        ).pack(side="left", padx=(12, 0))

        self._stamp_var = tk.StringVar(value="(no file open)")
        ttk.Label(top, textvariable=self._stamp_var, foreground="gray").pack(
            side="right"
        )

        # Body: canvas (left) + scrollable sidebar (right).
        body = ttk.Frame(self._parent)
        body.pack(fill="both", expand=True, padx=8, pady=4)

        self._canvas = tk.Canvas(
            body,
            width=CANVAS_WIDTH,
            height=CANVAS_HEIGHT,
            bg="white",
            highlightthickness=1,
            highlightbackground="#d0d0d3",
            cursor="crosshair",
        )
        self._canvas.pack(side="left", fill="both", expand=True)
        self._canvas.bind("<Button-1>", self._on_canvas_click)
        self._canvas.bind("<Shift-Button-1>", self._on_canvas_shift_click)

        # Sidebar with internal scrollable frame (ttk has no native scrollable).
        sidebar_outer = ttk.Frame(body, width=SIDEBAR_WIDTH)
        sidebar_outer.pack(side="right", fill="y", padx=(8, 0))
        sidebar_outer.pack_propagate(False)

        self._sidebar_canvas = tk.Canvas(
            sidebar_outer, width=SIDEBAR_WIDTH, highlightthickness=0, bg="#f5f5f7"
        )
        scrollbar = ttk.Scrollbar(
            sidebar_outer, orient="vertical", command=self._sidebar_canvas.yview
        )
        self._sidebar_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self._sidebar_canvas.pack(side="left", fill="both", expand=True)

        self._sidebar_frame = ttk.Frame(self._sidebar_canvas)
        self._sidebar_window = self._sidebar_canvas.create_window(
            (0, 0), window=self._sidebar_frame, anchor="nw", width=SIDEBAR_WIDTH
        )
        self._sidebar_frame.bind(
            "<Configure>",
            lambda _e: self._sidebar_canvas.configure(
                scrollregion=self._sidebar_canvas.bbox("all")
            ),
        )

        self._render_empty_sidebar()

    # ----------------------------------------------------------------- #
    # File I/O
    # ----------------------------------------------------------------- #

    def _on_open_svg(self) -> None:
        filename = filedialog.askopenfilename(
            title="Open SVG to edit",
            filetypes=[("SVG", "*.svg"), ("All files", "*.*")],
        )
        if not filename:
            return
        self._load_svg(Path(filename))

    def _load_svg(self, svg_path: Path) -> None:
        try:
            parsed = parse_svg_to_components(str(svg_path), target_width=100)
        except Exception as exc:
            messagebox.showerror("Parse error", f"Could not parse {svg_path.name}:\n{exc}")
            return
        if parsed is None:
            messagebox.showerror(
                "Parse error",
                f"No parseable geometry in {svg_path.name}.",
            )
            return
        components, svg_info = parsed
        # Detach from the previous project before swapping so its listener
        # list doesn't accidentally fire into a torn-down EditTab.
        if self._project is not None:
            self._project.off_change(self._on_project_change)

        # Prefer an existing sidecar over default body grouping so prior
        # edits aren't silently discarded — but verify the sidecar's
        # components match what we just parsed. If the SVG was re-saved
        # with different geometry while the sidecar lagged, the sidecar's
        # body→component_ids are stale and would render wrong; we discard
        # it (with a warning) and fall back to the default grouping.
        loaded = None
        try:
            candidate = ShapeProject.load_sidecar(svg_path)
        except ValueError as exc:
            self._log(f"[editor] ignoring corrupt sidecar: {exc}")
            candidate = None
        if candidate is not None:
            fresh_ids = {c.id for c in components}
            sidecar_ids = {c.id for c in candidate.components}
            if fresh_ids == sidecar_ids:
                loaded = candidate
            else:
                self._log(
                    f"[editor] sidecar for {svg_path.name} has stale components "
                    f"({len(sidecar_ids)} vs {len(fresh_ids)} parsed); regenerating"
                )

        if loaded is not None:
            self._project = loaded
            self._log(f"[editor] loaded sidecar for {svg_path.name}")
        else:
            self._project = ShapeProject.default_from_components(components, svg_info)
            self._log(f"[editor] {svg_path.name}: {len(components)} components")
        self._cache = BodyOutputCache(self._project)
        self._project.on_change(self._on_project_change)
        self._svg_path = svg_path
        self._stamp_var.set(f"{svg_path.name}  •  unsaved")
        self._render_all()

    def _save_now(self) -> None:
        if self._project is None or self._svg_path is None:
            return
        sidecar = self._project.save_sidecar(self._svg_path)
        self._stamp_var.set(f"{self._svg_path.name}  •  saved {sidecar.name}")

    def _on_autosave_toggle(self) -> None:
        if not self._autosave_var.get() and self._autosave_after_id:
            self._parent.after_cancel(self._autosave_after_id)
            self._autosave_after_id = None

    def _schedule_autosave(self) -> None:
        if not self._autosave_var.get():
            return
        if self._project is None or self._svg_path is None:
            return
        if self._autosave_after_id:
            self._parent.after_cancel(self._autosave_after_id)
        self._autosave_after_id = self._parent.after(
            AUTOSAVE_DELAY_MS, self._save_now
        )

    # ----------------------------------------------------------------- #
    # Model event hook
    # ----------------------------------------------------------------- #

    def _on_project_change(self, event_type: str, payload: dict) -> None:
        # Cache invalidates itself; the editor just redraws.
        self._render_all()
        # Sidecar only needs writing when project content (not selection) changes.
        if event_type in (EVENT_BODY_CHANGED, EVENT_TOPOLOGY_CHANGED):
            self._schedule_autosave()

    # ----------------------------------------------------------------- #
    # Canvas rendering + click handling
    # ----------------------------------------------------------------- #

    def _render_all(self) -> None:
        self._render_canvas()
        self._render_sidebar()

    def _render_canvas(self) -> None:
        self._canvas.delete("all")
        self._canvas_components.clear()
        project = self._project
        if project is None or not project.components:
            return

        # Compute the bounds across all component exteriors.
        xs, ys = [], []
        for c in project.components:
            for x, y in c.exterior:
                xs.append(x)
                ys.append(y)
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        w = max_x - min_x
        h = max_y - min_y
        if w <= 0 or h <= 0:
            return

        canvas_w = self._canvas.winfo_width() or CANVAS_WIDTH
        canvas_h = self._canvas.winfo_height() or CANVAS_HEIGHT
        pad = 12
        scale = min((canvas_w - 2 * pad) / w, (canvas_h - 2 * pad) / h)
        ox = pad + (canvas_w - 2 * pad - w * scale) / 2
        oy = pad + (canvas_h - 2 * pad - h * scale) / 2
        self._scale = scale
        self._offset = (ox, oy)
        self._origin = (min_x, min_y)

        body_for_cid = {
            cid: body
            for body in project.bodies
            for cid in body.component_ids
        }
        selected = project.selected_component_ids

        for component in project.components:
            body = body_for_cid.get(component.id)
            fill = (
                "#d0d0d3"
                if body is not None and not body.enabled
                else (body.color_hex if body and body.color_hex else
                      component.source_fill or DEFAULT_COLOR)
            )
            outline = "#007aff" if component.id in selected else "#1d1d1f"
            width = 3 if component.id in selected else 1
            coords = self._pixel_coords(component.exterior)
            item = self._canvas.create_polygon(
                coords, fill=fill, outline=outline, width=width
            )
            self._canvas_components[component.id] = item
            # Cut holes by drawing each as a white polygon on top. Tkinter
            # canvases can't render polygons-with-holes, so this is a
            # cosmetic approximation — the underlying ShapeProject knows
            # the real geometry; the visual is just a preview.
            for hole in component.holes:
                self._canvas.create_polygon(
                    self._pixel_coords(hole),
                    fill="white",
                    outline="#9ca3af",
                    width=1,
                )

        # Pyramid centers per enabled body.
        if self._cache is not None:
            for body in project.bodies:
                if not body.enabled:
                    continue
                for pos in self._cache.positions_for(body.id):
                    px, py = self._to_pixel(pos[0], pos[1])
                    self._canvas.create_oval(
                        px - 1.4, py - 1.4, px + 1.4, py + 1.4,
                        fill="#1d1d1f",
                        outline="",
                    )

    def _pixel_coords(self, ring) -> list[float]:
        flat: list[float] = []
        for x, y in ring:
            px, py = self._to_pixel(x, y)
            flat.extend([px, py])
        return flat

    def _to_pixel(self, x: float, y: float) -> tuple[float, float]:
        ox, oy = self._offset
        mx, my = self._origin
        return ox + (x - mx) * self._scale, oy + (y - my) * self._scale

    def _from_pixel(self, px: float, py: float) -> tuple[float, float]:
        ox, oy = self._offset
        mx, my = self._origin
        return (px - ox) / self._scale + mx, (py - oy) / self._scale + my

    def _on_canvas_click(self, event) -> None:
        if self._project is None:
            return
        x, y = self._from_pixel(event.x, event.y)
        hit = self._project.hit_test(x, y)
        if hit is None:
            self._project.clear_selection()
        else:
            self._project.select_component(hit)

    def _on_canvas_shift_click(self, event) -> None:
        if self._project is None:
            return
        x, y = self._from_pixel(event.x, event.y)
        hit = self._project.hit_test(x, y)
        if hit is not None:
            self._project.shift_select_component(hit)

    # ----------------------------------------------------------------- #
    # Sidebar rendering — one row per body
    # ----------------------------------------------------------------- #

    def _render_empty_sidebar(self) -> None:
        for child in self._sidebar_frame.winfo_children():
            child.destroy()
        ttk.Label(
            self._sidebar_frame,
            text="Open an SVG to start editing.",
            foreground="gray",
            wraplength=SIDEBAR_WIDTH - 24,
        ).pack(padx=12, pady=24)

    def _render_sidebar(self) -> None:
        for child in self._sidebar_frame.winfo_children():
            child.destroy()
        project = self._project
        if project is None or not project.bodies:
            self._render_empty_sidebar()
            return

        header = ttk.Label(
            self._sidebar_frame,
            text=f"{len(project.components)} components · {len(project.bodies)} bodies",
            foreground="gray",
        )
        header.pack(anchor="w", padx=8, pady=(8, 4))

        pattern_names = patterns.names()
        for body in project.bodies:
            self._render_body_row(body, pattern_names)

        actions = ttk.Frame(self._sidebar_frame)
        actions.pack(fill="x", padx=8, pady=(8, 16))
        group_btn = ttk.Button(
            actions,
            text="Group selected",
            command=self._on_group_selected,
            state="normal" if len(project.selected_component_ids) >= 2 else "disabled",
        )
        group_btn.pack(side="left")
        clear_btn = ttk.Button(
            actions,
            text="Clear selection",
            command=project.clear_selection,
            state="normal" if project.selected_component_ids else "disabled",
        )
        clear_btn.pack(side="left", padx=(6, 0))

    def _render_body_row(self, body, pattern_names) -> None:
        row = ttk.Frame(self._sidebar_frame, relief="solid", borderwidth=1)
        row.pack(fill="x", padx=8, pady=3, ipady=4, ipadx=4)

        top = ttk.Frame(row)
        top.pack(fill="x")

        swatch = tk.Frame(
            top, bg=body.color_hex or DEFAULT_COLOR, width=22, height=22,
            cursor="hand2", highlightthickness=1, highlightbackground="#9ca3af",
        )
        swatch.pack(side="left", padx=(2, 6))
        swatch.bind("<Button-1>", lambda _e, b=body: self._on_pick_color(b))

        name_var = tk.StringVar(value=body.name)
        ttk.Label(top, textvariable=name_var, font=("", 10, "bold")).pack(side="left")

        enabled_var = tk.BooleanVar(value=body.enabled)
        ttk.Checkbutton(
            top,
            text="on",
            variable=enabled_var,
            command=lambda b=body, v=enabled_var: self._project.set_body_enabled(
                b.id, v.get()
            ),
        ).pack(side="right")

        bottom = ttk.Frame(row)
        bottom.pack(fill="x", pady=(4, 0))

        ttk.Label(bottom, text="pattern", foreground="gray").pack(side="left", padx=(4, 2))
        pattern_var = tk.StringVar(value=body.pattern)
        pattern_box = ttk.Combobox(
            bottom,
            textvariable=pattern_var,
            values=pattern_names,
            state="readonly",
            width=11,
        )
        pattern_box.pack(side="left")
        pattern_box.bind(
            "<<ComboboxSelected>>",
            lambda _e, b=body, v=pattern_var: self._project.set_body_pattern(
                b.id, v.get()
            ),
        )

        ttk.Label(bottom, text="size", foreground="gray").pack(side="left", padx=(10, 2))
        size_var = tk.StringVar(
            value=str(body.pyramid_overrides.get("pyramid_size", ""))
        )
        size_entry = ttk.Entry(bottom, textvariable=size_var, width=5)
        size_entry.pack(side="left")
        size_entry.bind(
            "<FocusOut>", lambda _e, b=body, v=size_var: self._apply_size_override(b, v)
        )
        size_entry.bind(
            "<Return>", lambda _e, b=body, v=size_var: self._apply_size_override(b, v)
        )

    def _apply_size_override(self, body, var: tk.StringVar) -> None:
        raw = var.get().strip()
        if raw == "":
            self._project.set_body_pyramid_overrides(body.id, {})
            return
        try:
            value = float(raw)
        except ValueError:
            self._log(f"[editor] ignoring non-numeric override: {raw!r}")
            var.set(str(body.pyramid_overrides.get("pyramid_size", "")))
            return
        self._project.set_body_pyramid_overrides(body.id, {"pyramid_size": value})

    def _on_pick_color(self, body) -> None:
        chosen = colorchooser.askcolor(
            color=body.color_hex or DEFAULT_COLOR, title=f"Color for {body.name}"
        )
        if chosen and chosen[1]:
            self._project.set_body_color(body.id, chosen[1])

    def _on_group_selected(self) -> None:
        if self._project is None or not self._project.selected_component_ids:
            return
        try:
            self._project.group_selected_into_new_body()
        except ValueError as exc:
            messagebox.showinfo("Group", str(exc))
