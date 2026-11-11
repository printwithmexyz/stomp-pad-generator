"""Shared geometry package for the stomp pad generator (desktop and web)."""

from .refine import (
    CleanupSettings,
    PrintWarning,
    apply_overrides,
    clean_regions,
    merge_similar_fills,
    printability_warnings,
    scale_regions_to_mm,
)
from .svg_parse import ParsedSvg, Region, normalize_color, parse_svg

__all__ = [
    'CleanupSettings',
    'ParsedSvg',
    'PrintWarning',
    'Region',
    'apply_overrides',
    'clean_regions',
    'merge_similar_fills',
    'normalize_color',
    'parse_svg',
    'printability_warnings',
    'scale_regions_to_mm',
]
