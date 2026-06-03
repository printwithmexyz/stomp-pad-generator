"""ShapeProject — the cross-frontend contract.

A ``ShapeProject`` is the serializable state both the desktop and web
editors round-trip through: the components parsed from the SVG, the bodies
those components are grouped into, per-body color/pattern/overrides, and
the global pyramid parameters. Phase 2's editor will mutate one of these
and re-emit it; Phase 3's exporters consume it to produce per-body STLs
and a generic 3MF.

The model is intentionally JSON-first: ``to_json`` / ``from_json`` are the
only stable shape, and ``SCHEMA_VERSION`` lets future phases migrate older
sidecar files (``<svg-stem>.project.json``) instead of breaking them.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Optional

from .geometry import Component


# Bump when from_json needs migration. Phase 1.4 is v1 — the first version
# users will see on disk via the Phase 2.3 sidecar convention.
SCHEMA_VERSION = 1

# Default pyramid pattern when no body-level override is set. Matches the
# Phase 0 behavior (skeleton-following hex packing) so single-body imports
# render identically to v1 until the editor is wired up.
DEFAULT_PATTERN = "skeleton"


@dataclass
class Body:
    """A group of one or more Components that print as one colored region.

    ``component_ids`` references :class:`Component.id` values from the same
    :class:`ShapeProject`. ``color_hex`` defaults to the imported SVG fill
    of the largest-area component in the group (per the v2 plan's
    mixed-fill grouping rule). ``pattern`` is a registry key resolved by
    Phase 1.3's pattern module. ``pyramid_overrides`` carries per-body
    overrides (e.g. ``{"pyramid_size": 2.5}``) — empty means "use
    ``ShapeProject.global_params``".
    """

    id: int
    name: str
    component_ids: list[int]
    color_hex: Optional[str] = None
    enabled: bool = True
    pattern: str = DEFAULT_PATTERN
    pyramid_overrides: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Body":
        return cls(
            id=d["id"],
            name=d["name"],
            component_ids=list(d.get("component_ids", [])),
            color_hex=d.get("color_hex"),
            enabled=d.get("enabled", True),
            pattern=d.get("pattern", DEFAULT_PATTERN),
            pyramid_overrides=dict(d.get("pyramid_overrides", {})),
        )


@dataclass
class ShapeProject:
    """The full editable state of one pad: components + bodies + params.

    ``svg_info`` mirrors what :func:`stomppad.geometry.parse_svg_to_components`
    returns so renderers don't have to re-parse the SVG. ``global_params``
    holds the pyramid defaults (size, spacing, base_thickness, etc.) that
    today's parameter form already produces.
    """

    schema_version: int = SCHEMA_VERSION
    components: list[Component] = field(default_factory=list)
    bodies: list[Body] = field(default_factory=list)
    global_params: dict = field(default_factory=dict)
    svg_info: dict = field(default_factory=dict)

    @classmethod
    def default_from_components(
        cls,
        components: list[Component],
        svg_info: dict,
        global_params: Optional[dict] = None,
    ) -> "ShapeProject":
        """Build a project with one body per top-level component.

        The v2 plan's "resolved" section locks the defaults:
          - each top-level component → its own body (no auto-grouping)
          - body color = the imported source_fill of the *largest-area*
            component in the group (trivially the component itself here)
          - default pattern = skeleton
          - all bodies enabled
        """
        bodies = [
            Body(
                id=i,
                name=f"body_{i}",
                component_ids=[comp.id],
                color_hex=comp.source_fill,
                enabled=True,
                pattern=DEFAULT_PATTERN,
                pyramid_overrides={},
            )
            for i, comp in enumerate(components)
        ]
        return cls(
            schema_version=SCHEMA_VERSION,
            components=list(components),
            bodies=bodies,
            global_params=dict(global_params or {}),
            svg_info=dict(svg_info),
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "components": [c.to_dict() for c in self.components],
            "bodies": [b.to_dict() for b in self.bodies],
            "global_params": self.global_params,
            "svg_info": self.svg_info,
        }

    def to_json(self, indent: Optional[int] = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: dict) -> "ShapeProject":
        version = d.get("schema_version")
        if version is None:
            raise ValueError(
                "ShapeProject JSON missing required 'schema_version' field."
            )
        if version > SCHEMA_VERSION:
            raise ValueError(
                f"ShapeProject schema_version {version} is newer than this "
                f"build supports (max {SCHEMA_VERSION}). Upgrade stomppad."
            )
        if version < 1:
            raise ValueError(
                f"ShapeProject schema_version {version} is below the minimum "
                f"supported (1). The field exists from day one — anything "
                f"older is corrupt."
            )
        # Future Phase 2/3 versions branch here and migrate old shapes forward
        # before reconstructing the dataclass.
        return cls(
            schema_version=version,
            components=[Component.from_dict(c) for c in d.get("components", [])],
            bodies=[Body.from_dict(b) for b in d.get("bodies", [])],
            global_params=dict(d.get("global_params", {})),
            svg_info=dict(d.get("svg_info", {})),
        )

    @classmethod
    def from_json(cls, s: str) -> "ShapeProject":
        return cls.from_dict(json.loads(s))
