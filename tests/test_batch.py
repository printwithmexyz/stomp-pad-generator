import json
import os
import stat

import pytest

import zipfile

from stomppad.batch import (
    body_stl_path,
    export_3mf,
    load_or_build_design,
    process_svg,
    render_bodies,
    render_stl,
    save_debug_visualization,
)
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
    load_or_build_design(workspace / 'logo.svg', settings, cache, log.append)
    assert '  Using cached design' not in log
    assert sum('pyramids' in line for line in log) == 2
    load_or_build_design(workspace / 'logo.svg', settings, cache, log.append)
    assert log.count('  Using cached design') == 1


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


@pytest.mark.parametrize('payload', ['[]', 'null', '{"key": ', '"design"'])
def test_cache_holding_the_wrong_kind_of_data_is_rebuilt(workspace, payload):
    cache, log = workspace / 'cache', []
    load_or_build_design(workspace / 'logo.svg', SETTINGS, cache, log.append)
    (cache / 'logo' / 'design.json').write_text(payload)
    design = load_or_build_design(workspace / 'logo.svg', SETTINGS, cache, log.append)
    assert any('Cache unreadable' in line for line in log)
    assert len(design.positions()) > 0


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


TETRAHEDRON_STL = 'solid t\n' + ''.join(
    'facet normal 0 0 0\nouter loop\n' + ''.join(f'vertex {corner}\n' for corner in facet) + 'endloop\nendfacet\n'
    for facet in (('0 0 0', '0 1 0', '1 0 0'), ('0 0 0', '1 0 0', '0 0 1'),
                  ('0 0 0', '0 0 1', '0 1 0'), ('1 0 0', '0 1 0', '0 0 1'))
) + 'endsolid t\n'
WRITE_AN_STL = f"cat > \"$2\" <<'EOF'\n{TETRAHEDRON_STL}EOF\n"


@needs_shell_scripts
def test_render_stl_runs_openscad_with_output_then_input(workspace):
    openscad = fake_openscad(workspace / 'openscad', WRITE_AN_STL + 'echo "$@" > "$2.args"\n')
    log = []
    assert render_stl(workspace / 'pad.scad', workspace / 'pad.stl', openscad, log.append)
    assert (workspace / 'pad.stl.args').read_text().split() == [
        '-o', str(workspace / 'pad.stl'), str(workspace / 'pad.scad'),
    ]
    assert log == ['  STL file: pad.stl']


@needs_shell_scripts
def test_render_stl_surfaces_openscad_errors(workspace):
    openscad = fake_openscad(workspace / 'openscad', 'echo "syntax error" >&2\nexit 1\n')
    log = []
    assert not render_stl(workspace / 'pad.scad', workspace / 'pad.stl', openscad, log.append)
    assert log == ['  OpenSCAD error: syntax error']


@needs_shell_scripts
def test_render_stl_fails_when_openscad_writes_nothing(workspace):
    openscad = fake_openscad(workspace / 'openscad', 'exit 0\n')
    log = []
    assert not render_stl(workspace / 'pad.scad', workspace / 'pad.stl', openscad, log.append)
    assert any('wrote no STL' in line for line in log)


@needs_shell_scripts
def test_render_stl_can_ask_for_one_body(workspace):
    openscad = fake_openscad(workspace / 'openscad', WRITE_AN_STL + 'echo "$@" > "$2.args"\n')
    assert render_stl(workspace / 'pad.scad', workspace / 'pad.stl', openscad, lambda line: None, part=2)
    assert (workspace / 'pad.stl.args').read_text().split() == [
        '-o', str(workspace / 'pad.stl'), '-D', 'part=2', str(workspace / 'pad.scad'),
    ]


@needs_shell_scripts
def test_render_stl_fails_when_openscad_leaves_a_shape_out(workspace):
    openscad = fake_openscad(
        workspace / 'openscad',
        WRITE_AN_STL + 'echo "ERROR: The given mesh is not closed! Unable to convert." >&2\n',
    )
    log = []
    assert not render_stl(workspace / 'pad.scad', workspace / 'pad.stl', openscad, log.append)
    assert log == ['  OpenSCAD left part of the model out: ERROR: The given mesh is not closed! Unable to convert.']
    assert not (workspace / 'pad.stl').exists()


@needs_shell_scripts
def test_render_stl_fails_on_an_stl_with_nothing_in_it(workspace):
    openscad = fake_openscad(workspace / 'openscad', 'printf "solid empty\\nendsolid empty\\n" > "$2"\n')
    log = []
    assert not render_stl(workspace / 'pad.scad', workspace / 'pad.stl', openscad, log.append)
    assert any('wrote no STL' in line for line in log)
    assert not (workspace / 'pad.stl').exists()


TWO_TONE = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50">'
            '<rect width="60" height="50" fill="red"/><rect x="60" width="40" height="50" fill="blue"/></svg>')
@pytest.fixture
def two_tone(workspace):
    (workspace / 'logo.svg').write_text(TWO_TONE)
    settings = PadSettings(target_width=60, multicolor=True)
    design = load_or_build_design(workspace / 'logo.svg', settings, None, lambda line: None)
    return design, workspace / 'out' / 'logo.scad'


@needs_shell_scripts
def test_bodies_render_to_one_stl_each_and_pack_into_a_3mf(workspace, two_tone):
    design, scad_path = two_tone
    openscad = fake_openscad(workspace / 'openscad', WRITE_AN_STL)
    log = []
    stl_paths = render_bodies(design, scad_path, openscad, log.append)
    assert {number: path.name for number, path in stl_paths.items()} == {
        1: 'logo_1_base_ff0000.stl', 2: 'logo_2_color_0000ff.stl',
    }
    assert all(path.exists() for path in stl_paths.values())

    path = export_3mf(design, stl_paths, workspace / 'out' / 'logo.3mf', log.append)
    assert log[-1] == '  3MF file: logo.3mf'
    with zipfile.ZipFile(path) as archive:
        settings = archive.read('Metadata/model_settings.config').decode()
    assert settings.count('<part ') == 2
    assert 'value="2 #0000ff"' in settings


@needs_shell_scripts
def test_body_rendering_stops_at_the_first_failure_and_leaves_no_stl(workspace, two_tone):
    design, scad_path = two_tone
    fails_on_the_second_body = (
        WRITE_AN_STL + 'echo "$4" >> "$(dirname "$2")/calls"\n[ "$4" = "part=1" ] || exit 1\n'
    )
    openscad = fake_openscad(workspace / 'openscad', fails_on_the_second_body)
    assert render_bodies(design, scad_path, openscad, lambda line: None) is None
    assert (workspace / 'out' / 'calls').read_text().split() == ['part=1', 'part=2']
    assert not any(body_stl_path(scad_path, body).exists() for body in design.bodies)


def test_cached_design_keeps_its_bodies(workspace):
    (workspace / 'logo.svg').write_text(TWO_TONE)
    settings = PadSettings(target_width=60, multicolor=True, base_color='#000000')
    cache = workspace / 'cache'
    built = load_or_build_design(workspace / 'logo.svg', settings, cache, lambda line: None)
    cached = load_or_build_design(workspace / 'logo.svg', settings, cache, lambda line: None)
    assert [body.label for body in cached.bodies] == ['1 base #000000', '2 #ff0000', '3 #0000ff']
    assert generate_openscad(cached) == generate_openscad(built)


def test_debug_visualization_is_written(workspace):
    pytest.importorskip('matplotlib')
    design = load_or_build_design(workspace / 'logo.svg', SETTINGS, None, lambda line: None)
    picture = workspace / 'debug.png'
    assert save_debug_visualization(design, picture, lambda line: None)
    assert picture.stat().st_size > 10_000
