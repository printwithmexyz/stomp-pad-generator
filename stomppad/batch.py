"""Turn SVG files on disk into .scad, .stl and .3mf files, with an on-disk cache. No GUI imports."""

import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np
import shapely

from .openscad import generate_openscad
from .packing import PackedRegion, pyramid_footprints
from .pad import PadDesign, build_pad
from .overlay import polygon_parts
from .svg_parse import Region
from .threemf import pad_3mf

CACHE_VERSION = 3  # bump whenever the pipeline can produce a different design for the same input
STL_RENDER_TIMEOUT_SECONDS = 300


def _cache_key(svg_file, settings):
    digest = hashlib.sha256()
    digest.update(Path(svg_file).read_bytes())
    digest.update(f'{CACHE_VERSION}|{settings!r}'.encode())
    return digest.hexdigest()


def _design_to_json(design, key):
    return {
        'key': key,
        'regions': [
            {'id': region.id, 'fill': region.fill, 'geometry': region.geometry.wkb_hex}
            for region in design.regions
        ],
        'art': design.art.wkb_hex,
        'voids': design.voids.wkb_hex,
        'packed': {
            str(region_id): {
                'positions': packed.positions.tolist(),
                'skeleton': packed.skeleton.tolist(),
            }
            for region_id, packed in design.packed.items()
        },
        'warnings': design.warnings,
    }


def _design_from_json(data, settings):
    regions = [
        Region(entry['id'], entry['fill'], shapely.from_wkb(entry['geometry']))
        for entry in data['regions']
    ]
    packed = {
        int(region_id): PackedRegion(
            np.array(entry['positions'], dtype=float).reshape(-1, 3),
            np.array(entry['skeleton'], dtype=float).reshape(-1, 2),
        )
        for region_id, entry in data['packed'].items()
    }
    return PadDesign(
        settings, regions, shapely.from_wkb(data['art']), shapely.from_wkb(data['voids']),
        packed, data['warnings'],
    )


def _read_cached_design(cache_file, key, settings, log):
    try:
        data = json.loads(cache_file.read_text())
        if not isinstance(data, dict):
            raise ValueError('not a design record')
        if data.get('key') != key:
            return None
        return _design_from_json(data, settings)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, shapely.errors.ShapelyError) as error:
        log(f'  Cache unreadable, rebuilding: {error}')
        return None


def load_or_build_design(svg_file, settings, cache_dir=None, log=print):
    """
    Build the pad design for an SVG, reusing the cached one when neither the
    file nor any setting has changed. Pass cache_dir=None to skip the cache.
    """
    if cache_dir is None:
        return build_pad(svg_file, settings, logger=log)

    cache_file = Path(cache_dir) / Path(svg_file).stem / 'design.json'
    key = _cache_key(svg_file, settings)
    if cache_file.exists():
        design = _read_cached_design(cache_file, key, settings, log)
        if design is not None:
            log('  Using cached design')
            for line in design.describe():
                log(line)
            return design

    design = build_pad(svg_file, settings, logger=log)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(_design_to_json(design, key)))
    return design


def process_svg(svg_file, output_dir, settings, cache_dir=None, log=print, approve=None):
    """
    Write <stem>.scad for an SVG into output_dir and return its path.

    approve, if given, is called with the design before anything is written.
    Returning False skips the file, and process_svg returns None.
    """
    svg_file = Path(svg_file)
    design = load_or_build_design(svg_file, settings, cache_dir, log)
    if approve is not None and not approve(design):
        return None
    scad_path = Path(output_dir) / f'{svg_file.stem}.scad'
    scad_path.write_text(generate_openscad(design))
    log(f'  OpenSCAD file: {scad_path.name}')
    return scad_path


def render_stl(scad_path, stl_path, openscad_path, log=print, part=None):
    """
    Render a .scad to .stl with the OpenSCAD executable. Returns True on
    success. part picks one body of the pad. Left as None, the whole pad
    renders in one piece.
    """
    command = [str(openscad_path), '-o', str(stl_path)]
    if part is not None:
        command += ['-D', f'part={part}']
    try:
        result = subprocess.run(
            [*command, str(scad_path)],
            capture_output=True, text=True, timeout=STL_RENDER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        log('  Timeout rendering STL')
        return False
    except FileNotFoundError:
        log(f'  OpenSCAD executable not found: {openscad_path}')
        return False
    except OSError as error:
        log(f'  STL render error: {error}')
        return False
    if result.returncode != 0:
        log(f'  OpenSCAD error: {result.stderr.strip()}')
        return False
    # OpenSCAD exits cleanly after leaving a shape it could not process out of
    # the model, so the STL exists but is missing material.
    dropped = [line for line in result.stderr.splitlines() if line.startswith('ERROR:')]
    if dropped:
        log(f'  OpenSCAD left part of the model out: {dropped[0]}')
        return False
    stl_path = Path(stl_path)
    if not stl_path.exists() or stl_path.stat().st_size == 0:
        log(f'  OpenSCAD finished but wrote no STL. {result.stderr.strip()}')
        return False
    log(f'  STL file: {stl_path.name}')
    return True


def body_stl_path(scad_path, body):
    scad_path = Path(scad_path)
    role = 'base' if body.is_base else 'color'
    return scad_path.with_name(f'{scad_path.stem}_{body.number}_{role}_{body.fill.lstrip("#")}.stl')


def render_bodies(design, scad_path, openscad_path, log=print):
    """
    Render one STL per body next to the .scad. Returns {body number: path},
    or None if any render failed.
    """
    paths = {}
    for body in design.bodies:
        stl_path = body_stl_path(scad_path, body)
        if not render_stl(scad_path, stl_path, openscad_path, log, part=body.number):
            return None
        paths[body.number] = stl_path
    return paths


def export_3mf(design, stl_paths, output_path, log=print, standard_colors=False):
    """Pack the body STLs from render_bodies into one 3MF with a part per filament."""
    output_path = Path(output_path)
    stl_by_body = {number: Path(path).read_bytes() for number, path in stl_paths.items()}
    output_path.write_bytes(pad_3mf(design, stl_by_body, output_path.stem, standard_colors))
    log(f'  3MF file: {output_path.name}')
    return output_path


def save_debug_visualization(design, output_file, log=print):
    """Save a picture of the art, holes, centerline and pyramid bases. Needs matplotlib."""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        log('  matplotlib not available for debug visualization')
        return False

    figure, axes = plt.subplots(figsize=(12, 10))
    for polygon in polygon_parts(design.art):
        axes.fill(*polygon.exterior.xy, color='#cfe3f7', zorder=1)
        for hole in polygon.interiors:
            axes.fill(*hole.xy, color='white', zorder=2)
        axes.plot(*polygon.exterior.xy, color='#1f5fa8', linewidth=1.5, zorder=4)
    for index, void in enumerate(polygon_parts(design.voids)):
        axes.fill(*void.exterior.xy, facecolor='none', edgecolor='#9a9a9a', hatch='///',
                  linewidth=0.8, zorder=3, label='Hole' if index == 0 else None)
    for index, packed in enumerate(design.packed.values()):
        if len(packed.skeleton):
            axes.scatter(packed.skeleton[:, 0], packed.skeleton[:, 1], c='green', s=1, alpha=0.5,
                         zorder=5, label='Centerline' if index == 0 else None)
    packing = design.settings.packing
    footprints = pyramid_footprints(design.positions(), packing.pyramid_size, packing.pyramid_style)
    for index, footprint in enumerate(footprints):
        axes.plot(*footprint.exterior.xy, color='red', linewidth=0.6, zorder=6,
                  label='Pyramid base' if index == 0 else None)

    axes.set_aspect('equal')
    axes.grid(True, alpha=0.3)
    axes.legend(loc='upper right')
    axes.set_title(f'{design.width:.1f} x {design.height:.1f} mm, {len(footprints)} pyramids')
    figure.savefig(output_file, dpi=150, bbox_inches='tight')
    plt.close(figure)
    log(f'  Debug visualization saved: {Path(output_file).name}')
    return True
