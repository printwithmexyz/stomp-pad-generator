import json
import os
import stat

import pytest

from stomppad.batch import load_or_build_design, process_svg, render_stl, save_debug_visualization
from stomppad.openscad import generate_openscad
from stomppad.pad import PadSettings

DONUT = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
         '<path fill-rule="evenodd" d="M50 5 A45 45 0 1 0 50 95 A45 45 0 1 0 50 5 Z '
         'M50 25 A25 25 0 1 0 50 75 A25 25 0 1 0 50 25 Z"/></svg>')
SETTINGS = PadSettings(target_width=60)
needs_shell_scripts = pytest.mark.skipif(os.name == 'nt', reason='fake OpenSCAD is a shell script')


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / 'logo.svg').write_text(DONUT)
    (tmp_path / 'out').mkdir()
    return tmp_path


def test_process_svg_writes_a_standalone_scad(workspace):
    log = []
    scad_path = process_svg(workspace / 'logo.svg', workspace / 'out', SETTINGS, log=log.append)
    assert scad_path == workspace / 'out' / 'logo.scad'
    assert 'import(' not in scad_path.read_text()
    assert [path.name for path in (workspace / 'out').iterdir()] == ['logo.scad']
    assert any('pyramids' in line for line in log)


def test_declined_approval_writes_nothing(workspace):
    seen = []
    result = process_svg(workspace / 'logo.svg', workspace / 'out', SETTINGS, log=seen.append,
                         approve=lambda design: seen.append(design) or False)
    assert result is None
    assert list((workspace / 'out').iterdir()) == []
    assert len(seen[-1].positions()) > 0


def test_cached_design_reproduces_the_same_scad(workspace):
    cache, log = workspace / 'cache', []
    built = load_or_build_design(workspace / 'logo.svg', SETTINGS, cache, log.append)
    cached = load_or_build_design(workspace / 'logo.svg', SETTINGS, cache, log.append)
    assert (cache / 'logo' / 'design.json').exists()
    assert log.count('  Using cached design') == 1
    assert generate_openscad(cached) == generate_openscad(built)
    assert cached.warnings == built.warnings


def test_cache_hit_still_reports_notes(workspace):
    cache, first, second = workspace / 'cache', [], []
    settings = PadSettings(target_width=60, void_mode='recess', recess_depth=5)
    load_or_build_design(workspace / 'logo.svg', settings, cache, first.append)
    load_or_build_design(workspace / 'logo.svg', settings, cache, second.append)
    assert any('cut through' in line for line in first)
    assert second[1:] == first


@pytest.mark.parametrize('change', ['setting', 'file'])
def test_cache_misses_when_anything_changes(workspace, change):
    cache, log = workspace / 'cache', []
    load_or_build_design(workspace / 'logo.svg', SETTINGS, cache, log.append)
    settings = SETTINGS
    if change == 'setting':
        settings = PadSettings(target_width=60, void_mode='recess')
    else:
        (workspace / 'logo.svg').write_text(DONUT.replace('r="45"', '').replace('A45 45', 'A45 44'))
    design = load_or_build_design(workspace / 'logo.svg', settings, cache, log.append)
    assert '  Using cached design' not in log
    assert design.settings == settings


def test_corrupt_cache_is_rebuilt(workspace):
    cache, log = workspace / 'cache', []
    load_or_build_design(workspace / 'logo.svg', SETTINGS, cache, log.append)
    cache_file = cache / 'logo' / 'design.json'
    stored = json.loads(cache_file.read_text())
    stored['art'] = 'not geometry'
    cache_file.write_text(json.dumps(stored))
    design = load_or_build_design(workspace / 'logo.svg', SETTINGS, cache, log.append)
    assert any('Cache unreadable' in line for line in log)
    assert design.art.area > 0
    assert json.loads(cache_file.read_text())['art'] != 'not geometry'


def test_no_cache_dir_means_no_cache(workspace):
    load_or_build_design(workspace / 'logo.svg', SETTINGS, None, lambda line: None)
    assert not (workspace / 'cache').exists()


def test_render_stl_reports_a_missing_executable(workspace):
    log = []
    assert not render_stl('pad.scad', 'pad.stl', workspace / 'no-such-openscad', log.append)
    assert any('not found' in line for line in log)


def fake_openscad(path, script):
    path.write_text('#!/bin/sh\n' + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@needs_shell_scripts
def test_render_stl_runs_openscad_with_output_then_input(workspace):
    openscad = fake_openscad(workspace / 'openscad', 'echo "$1 $3" > "$2"\n')
    log = []
    assert render_stl(workspace / 'pad.scad', workspace / 'pad.stl', openscad, log.append)
    assert (workspace / 'pad.stl').read_text().split() == ['-o', str(workspace / 'pad.scad')]
    assert log == ['  STL file: pad.stl']


@needs_shell_scripts
def test_render_stl_surfaces_openscad_errors(workspace):
    openscad = fake_openscad(workspace / 'openscad', 'echo "syntax error" >&2\nexit 1\n')
    log = []
    assert not render_stl(workspace / 'pad.scad', workspace / 'pad.stl', openscad, log.append)
    assert log == ['  OpenSCAD error: syntax error']


def test_debug_visualization_is_written(workspace):
    pytest.importorskip('matplotlib')
    design = load_or_build_design(workspace / 'logo.svg', SETTINGS, None, lambda line: None)
    picture = workspace / 'debug.png'
    assert save_debug_visualization(design, picture, lambda line: None)
    assert picture.stat().st_size > 10_000
