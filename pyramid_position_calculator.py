#!/usr/bin/env python3
"""Back-compat shim — the implementation moved to the ``stomppad`` package.

Phase 0 of the v2 plan packages the geometry pipeline into ``stomppad/``.
This module is kept so that ``import pyramid_position_calculator`` and
``python pyramid_position_calculator.py`` continue to work unchanged for
existing callers (the desktop GUI, frozen binaries, and any user scripts).
"""

from stomppad import *  # noqa: F401,F403
from stomppad import main


if __name__ == "__main__":
    main()
