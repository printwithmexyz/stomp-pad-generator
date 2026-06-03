"""Pattern registry for pyramid placement strategies.

A ``PatternStrategy`` generates *candidate* ``(x, y, rotation)`` positions
inside a component's solid geometry. The validation pass (footprint
inside safety-inset polygon) happens in :func:`stomppad.packing.pack_component`,
so strategies focus only on the *layout* logic and don't have to
re-implement boundary checks.

Strategies register themselves at import time via :func:`register`. A
:class:`stomppad.project.Body` references a strategy by name (``pattern=
"hexagonal"``); the model stays JSON-serializable and Phase 2's editor
just shows :func:`names` in a dropdown. Radial / concentric / custom
patterns can register later with no caller change.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

# Built-in strategies are imported below for their side effect (each
# module calls ``register``). New strategies can be added by importing
# this module and calling ``register("my_pattern", MyPattern())``.

_REGISTRY: dict[str, "PatternStrategy"] = {}


@runtime_checkable
class PatternStrategy(Protocol):
    """Generate candidate pyramid positions for one component's solid geometry.

    ``needs_skeleton`` lets the packer skip the medial-axis raster when
    the strategy doesn't use it — meaningful since the skeleton step is
    the slow path in Pyodide on multi-body files.
    """

    needs_skeleton: bool

    def generate_positions(
        self,
        *,
        solid_geom,
        pyramid_size: float,
        pyramid_spacing: float,
        include_rotation: bool,
        skeleton_points,
        logger,
    ) -> list[list[float]]:
        ...


def register(name: str, strategy: PatternStrategy) -> None:
    """Register ``strategy`` under ``name``. Last-write-wins on duplicate."""
    _REGISTRY[name] = strategy


def get(name: str) -> PatternStrategy:
    """Look up a strategy by name. Raises :class:`KeyError` if unknown."""
    try:
        return _REGISTRY[name]
    except KeyError as exc:  # surface a useful list rather than just the key
        raise KeyError(
            f"Unknown pattern {name!r}. Registered: {names()}"
        ) from exc


def names() -> list[str]:
    """Sorted list of registered pattern names — useful for dropdowns."""
    return sorted(_REGISTRY.keys())


# --- Built-in strategies (registered on import) ----------------------------
from . import skeleton  # noqa: E402, F401
from . import hexagonal  # noqa: E402, F401
from . import rectangular  # noqa: E402, F401
from . import triangular  # noqa: E402, F401
