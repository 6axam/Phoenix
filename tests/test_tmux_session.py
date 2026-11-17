import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import pytest
from phoenix.adapters.tmux_session import TmuxError, collect_tmux


@pytest.fixture
def tmux_socket(tmp_path):
    if not shutil.which('tmux'): pytest.skip('tmux unavailable')
    folder = tmp_path / 'private'; folder.mkdir(mode=0o700)
    path = folder / 'tmux.sock'
    home = tmp_path / 'home'; home.mkdir()
    env = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(tmp_path/'config'), XDG_DATA_HOME=str(tmp_path/'data'), XDG_STATE_HOME=str(tmp_path/'state'))
    def tmux(*args):
        return subprocess.run(['tmux', '-S', str(path), '-f', '/dev/null', *args], env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    tmux('new-session', '-d', '-s', 'phoenix-test', '-c', str(tmp_path), '/bin/sh')
    try: yield path, tmux
    finally:
        if path.exists(): tmux('kill-server')


def test_real_tmux_metadata_for_multiple_windows_and_splits(tmux_socket, tmp_path):
    path, tmux = tmux_socket
    work = tmp_path / 'work'; work.mkdir()
    tmux('split-window', '-h', '-t', 'phoenix-test:0', '-c', str(work), '/bin/sh')
    tmux('new-window', '-t', 'phoenix-test:', '-n', 'project', '-c', str(work), '/bin/sh')
    result = collect_tmux(str(path))
    assert result['version'] == 1 and len(result['sessions']) == 1
    session = result['sessions'][0]
    assert session['name'] == 'phoenix-test' and len(session['windows']) == 2
    first, second = session['windows']
    assert len(first['panes']) == 2 and len(second['panes']) == 1
    assert {p['cwd'] for p in first['panes']} == {str(tmp_path), str(work)}
    assert second['name'] == 'project' and second['active']
    assert first['panes'][0]['x'] < first['panes'][1]['x']
    assert all(not any(k in pane for k in ('pid', 'command', 'env', 'id')) for window in session['windows'] for pane in window['panes'])
    assert len(collect_tmux(str(path))['sessions']) == 1


def test_tmux_rejects_socket_with_unsafe_directory(tmux_socket):
    path, _ = tmux_socket
    path.parent.chmod(0o755)
    with pytest.raises(TmuxError): collect_tmux(str(path))
    path.parent.chmod(0o700)


def test_tmux_rejects_races_and_malformed_rows(tmp_path, monkeypatch):
    directory = tmp_path/'private'; directory.mkdir(mode=0o700)
    path = directory/'sock'; sock = socket.socket(socket.AF_UNIX); sock.bind(str(path))
    os.chmod(path, 0o600)
    monkeypatch.setattr(shutil, 'which', lambda name: '/usr/bin/tmux')
    outputs = [b'$0\x1fs\x1f1\n', b'$0\x1f@0\x1f0\x1fw\x1f1\x1f80\x1f24\n', b'$0\x1f@0\x1f%0\x1f0\x1f1\x1f/tmp\x1f0\x1f0\x1f80\x1f24\n']
    try:
        calls = []
        def racing(args, limit, timeout):
            calls.append(args)
            return outputs[(len(calls) - 1) % 3] + (b'x' if len(calls) == 4 else b'')
        with pytest.raises(TmuxError, match='изменилось|символы'): collect_tmux(str(path), runner=racing)
        assert all('-S' in c and 'list-' in ' '.join(c) and not any(x in c for x in ('new-session', 'send-keys', 'kill-server')) for c in calls)
        def malformed(*_): return b'bad\x1econtrol\n'
        with pytest.raises(TmuxError): collect_tmux(str(path), runner=malformed)
    finally: sock.close()


def test_tmux_rejects_overlapping_geometry(tmp_path, monkeypatch):
    directory = tmp_path / 'private'; directory.mkdir(mode=0o700)
    path = directory / 'sock'; sock = socket.socket(socket.AF_UNIX); sock.bind(str(path)); os.chmod(path, 0o600)
    monkeypatch.setattr(shutil, 'which', lambda name: '/usr/bin/tmux')
    rows = [b'$0\x1fs\x1f1\n', b'$0\x1f@0\x1f0\x1fw\x1f1\x1f80\x1f24\n',
            b'$0\x1f@0\x1f%0\x1f0\x1f1\x1f/tmp\x1f0\x1f0\x1f80\x1f24\n$0\x1f@0\x1f%1\x1f1\x1f0\x1f/tmp\x1f0\x1f0\x1f20\x1f10\n']
    calls = []
    def runner(*args):
        calls.append(1); return rows[(len(calls) - 1) % 3]
    try:
        with pytest.raises(TmuxError, match='перекрываются'): collect_tmux(str(path), runner=runner)
    finally: sock.close()


def test_cli_tmux_inspection_does_not_open_phoenix_database(tmux_socket, tmp_path, monkeypatch, capsys):
    from phoenix.cli import main
    path, _ = tmux_socket
    data = tmp_path/'phoenix-data'; monkeypatch.setenv('XDG_DATA_HOME', str(data))
    assert main(['tmux-inspect', '--socket', str(path)]) == 0
    assert json.loads(capsys.readouterr().out)['sessions'][0]['name'] == 'phoenix-test'
    assert not data.exists()


def test_tmux_metadata_is_saved_only_when_enabled_and_failures_keep_windows(tmux_socket, tmp_path, monkeypatch):
    from phoenix.core import Collector, SnapshotManager
    from test_phoenix import FakeBackend, window
    path, _ = tmux_socket
    backend = FakeBackend([{'class': 'Example', 'initialClass': 'Example', 'workspace': {'id': 1}, 'mapped': True, 'fullscreen': 0}])
    off = Collector(backend, {'tmux_socket': str(path)}).collect()
    assert 'tmux' not in off
    on = Collector(backend, {'tmux_socket': str(path), 'tmux_metadata_enabled': True}).collect()
    assert on['tmux']['sessions'][0]['name'] == 'phoenix-test'
    manager = SnapshotManager(tmp_path / 'snapshot.sqlite3')
    snapshot = manager.save(on)
    assert manager.get(snapshot)[1]['tmux'] == on['tmux']
    bad = Collector(backend, {'tmux_socket': str(tmp_path / 'missing'), 'tmux_metadata_enabled': True})
    state = bad.collect()
    assert state['windows'] and 'tmux' not in state and bad.adapter_warnings
