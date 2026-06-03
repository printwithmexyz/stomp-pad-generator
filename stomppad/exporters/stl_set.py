"""STL-set exporter.

Phase 3.2: bundle per-body STL bytes (produced by the caller via OpenSCAD)
into a single dict ready for download, alongside a human-readable
``print-guide.txt`` that maps body name → color → filename.

The STL rendering itself lives outside this module — desktop hands the
per-body SCAD strings to a subprocess OpenSCAD, browser pipes them
through openscad-wasm. This module only knows about the project model and
the already-rendered bytes; that keeps it portable to either runtime.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from ..project import Body, ShapeProject


def _safe_filename(name: str) -> str:
    """Slug-ify a body name into a filesystem-safe filename stem.

    Lowercase ASCII alphanumerics, dashes for everything else, collapse
    runs of dashes. Empty result falls back to "body".
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-").lower()
    return cleaned or "body"


def _color_label(body: Body) -> str:
    return body.color_hex if body.color_hex else "(no color set)"


def _build_print_guide(project: ShapeProject, filename_for: dict[int, str]) -> str:
    """Render a Markdown-flavored guide so the slicer-operator knows which
    STL to load in which extruder."""
    lines = [
        "# Stomp Pad print guide",
        "",
        "One STL per enabled body. Load each into your slicer at the same",
        "XY origin and assign the listed color/material before slicing.",
        "",
    ]
    if project.svg_info:
        w = project.svg_info.get("final_width")
        h = project.svg_info.get("final_height")
        if w and h:
            lines.append(f"Source footprint: {w:.1f} mm × {h:.1f} mm")
            lines.append("")
    lines.append(f"| {'Body':<24} | {'Color':<10} | {'Pattern':<12} | File")
    lines.append(f"|{'-'*26}|{'-'*12}|{'-'*14}|{'-'*30}")
    for body in project.bodies:
        if body.id not in filename_for:
            continue
        lines.append(
            f"| {body.name:<24} | {_color_label(body):<10} | "
            f"{body.pattern:<12} | {filename_for[body.id]}"
        )
    skipped = [b for b in project.bodies if not b.enabled]
    if skipped:
        lines.append("")
        lines.append("Skipped (disabled):")
        for body in skipped:
            lines.append(f"  - {body.name} ({_color_label(body)})")
    lines.append("")
    return "\n".join(lines)


def build_stl_set(
    project: ShapeProject,
    stl_per_body: dict,
) -> dict[str, bytes]:
    """Return ``{filename: bytes}`` for an STL-set bundle.

    Each enabled body that has an entry in ``stl_per_body`` produces a
    ``<safe-body-name>.stl`` file. ``print-guide.txt`` summarizes the
    body→color mapping. Disabled bodies are noted in the guide but no STL
    is emitted for them.

    Accepts ``stl_per_body`` keyed by either ``int`` or ``str`` — JS objects
    coerced through ``pyodide.toPy`` arrive with string keys, and we'd
    silently produce an empty bundle if we required int keys.
    """
    stl_per_body = {int(k): v for k, v in stl_per_body.items()}
    out: dict[str, bytes] = {}
    filename_for: dict[int, str] = {}
    seen_names: set[str] = set()
    for body in project.bodies:
        if not body.enabled or body.id not in stl_per_body:
            continue
        stem = _safe_filename(body.name)
        # Disambiguate collisions: two bodies sharing a name get suffixed
        # by id so the slicer-loader can tell them apart.
        name = f"{stem}.stl"
        if name in seen_names:
            name = f"{stem}-{body.id}.stl"
        seen_names.add(name)
        out[name] = stl_per_body[body.id]
        filename_for[body.id] = name
    out["print-guide.txt"] = _build_print_guide(project, filename_for).encode("utf-8")
    return out


def write_stl_set(
    project: ShapeProject,
    stl_per_body: dict[int, bytes],
    out_dir,
) -> dict[str, Path]:
    """Convenience for the desktop side: write the bundle to ``out_dir``
    and return ``{filename: written_path}``. Creates the directory if it
    doesn't exist. The web frontend uses :func:`build_stl_set` directly
    (no filesystem)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle = build_stl_set(project, stl_per_body)
    paths: dict[str, Path] = {}
    for name, data in bundle.items():
        target = out_dir / name
        if isinstance(data, bytes):
            target.write_bytes(data)
        else:
            target.write_text(data, encoding="utf-8")
        paths[name] = target
    return paths
