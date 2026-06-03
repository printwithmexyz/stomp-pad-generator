"""Phase 3 multicolor exporters.

Two formats, both produced from a :class:`stomppad.project.ShapeProject` +
already-rendered per-body STLs (rendering is platform-specific —
subprocess to OpenSCAD on desktop, openscad-wasm in the browser — so it
stays outside this package):

- :mod:`stomppad.exporters.stl_set` — one STL per enabled body plus a
  ``print-guide.txt`` mapping body name → color → filename. Easiest path:
  the user loads each STL into the slicer manually and assigns its color
  from the guide.
- :mod:`stomppad.exporters.threemf` — single ``.3mf`` (zip) with one
  ``<object>`` per body and a base material colour per object. Standard
  3MF core schema with the material extension; imports into any modern
  slicer with caveats called out in the v2 plan §3.3.

Both expose ``build_*`` functions that return ``{filename: bytes}`` so
the caller can write them to disk (desktop) or hand them to the browser's
download API.
"""

from .stl_set import build_stl_set, write_stl_set  # noqa: F401
from .threemf import build_threemf  # noqa: F401

__all__ = ["build_stl_set", "write_stl_set", "build_threemf"]
