#!/usr/bin/env python3
"""Back-compat shim — the implementation moved to the ``stomppad`` package.

**New code should import :mod:`stomppad` directly** — ``from stomppad
import parse_svg_to_polygon`` and so on. This shim exists only so that
the standalone entry point (``python pyramid_position_calculator.py``)
and any pre-v2 user scripts that still write ``import
pyramid_position_calculator`` keep working. The desktop GUI was updated
to import from ``stomppad`` in v2 Phase 1; the web frontend never used
this module.
"""

from stomppad import *  # noqa: F401,F403
from stomppad import main


if __name__ == "__main__":
    main()
