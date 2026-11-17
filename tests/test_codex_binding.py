import base64
import json
import os
import pytest
from phoenix.adapters.codex_binding import VAR, collect_binding, resume_in_current_pane
from phoenix.adapters.codex_session import CodexCheckError

IDENT = 'a04f5046-8ea3-4c04-bda8-49fa089858c5'


def marker():
    return {'version': 1, 'session_id': IDENT, 'wrapper': 111, 'wrapper_start': 222, 'child': 333, 'child_start': 444}


def pane(tmp_path):
    return {'cwd': str(tmp_path), 'user_vars': {VAR: json.dumps(marker())}, 'foreground_processes': [{'pid': 333, 'cmdline': ['/usr/bin/codex', 'resume', IDENT], 'cwd': str(tmp_path)}]}


def test_binding_requires_matching_live_processes_and_never_persists_pids(tmp_path, monkeypatch):
    import phoenix.adapters.codex_binding as binding
    monkeypatch.setattr(binding, '_identity', lambda pid: {111: (1, 222), 333: (111, 444)}[pid])
    monkeypatch.setattr(binding, '_is_phoenix_wrapper', lambda pid: pid == 111)
    window = pane(tmp_path)
    assert collect_binding(window) == {'session_id': IDENT, 'source': 'explicit_resume'}
    for field, value in [('wrapper_start', 223), ('child_start', 445), ('child', 999), ('session_id', '--last')]:
        changed = marker(); changed[field] = value
        window['user_vars'][VAR] = json.dumps(changed)
        assert collect_binding(window) is None
    window = pane(tmp_path); window['foreground_processes'][0]['cmdline'].append('--dangerous')
    assert collect_binding(window) is None
    window = pane(tmp_path); window['foreground_processes'][0]['cwd'] = '/different'
    assert collect_binding(window) is None
    window = pane(tmp_path); window['foreground_processes'] = []
    assert collect_binding(window) is None


def test_explicit_resume_sets_and_clears_marker(tmp_path, monkeypatch):
    import phoenix.adapters.codex_binding as binding
    monkeypatch.setattr(binding.os, 'isatty', lambda fd: True)
    monkeypatch.setattr(binding.os, 'ttyname', lambda fd: '/dev/pts/test')
    monkeypatch.setenv('KITTY_WINDOW_ID', '44')
    monkeypatch.setattr(binding, 'check_session', lambda ident, cwd: {'session_id': ident, 'cwd': cwd})
    monkeypatch.setattr(binding.shutil, 'which', lambda _: '/usr/bin/codex')
    monkeypatch.setattr(binding.os, 'getpid', lambda: 111)
    monkeypatch.setattr(binding, '_identity', lambda pid: (0, {111: 222, 333: 444}[pid]))
    output = []; calls = []
    monkeypatch.setattr(binding.os, 'write', lambda fd, data: output.append(data) or len(data))
    class Child:
        pid = 333
        def wait(self, timeout=None): return 0
    monkeypatch.setattr(binding.subprocess, 'Popen', lambda args, **kw: calls.append((args, kw)) or Child())
    assert resume_in_current_pane(IDENT, str(tmp_path)) == 0
    assert calls == [(['/usr/bin/codex', 'resume', IDENT], {'cwd': str(tmp_path)})]
    encoded = output[0].split(b'=', 2)[2].removesuffix(b'\x07')
    assert json.loads(base64.b64decode(encoded)) == marker()
    assert output[1] == b'\x1b]1337;SetUserVar=phoenix_codex_binding\x07'


def test_explicit_resume_refuses_non_kitty_tty(monkeypatch, tmp_path):
    import phoenix.adapters.codex_binding as binding
    monkeypatch.setattr(binding.os, 'isatty', lambda fd: False)
    with pytest.raises(CodexCheckError, match='внутри панели Kitty'):
        resume_in_current_pane(IDENT, str(tmp_path))


def test_saved_binding_and_cli_restore_are_explicit_and_read_only(tmp_path, monkeypatch):
    from phoenix.adapters.codex_binding import saved_binding
    from phoenix.cli import main
    from phoenix.core import SnapshotManager
    data_dir = tmp_path / 'data'; monkeypatch.setenv('XDG_DATA_HOME', str(data_dir))
    manager = SnapshotManager()
    content = {'windows': [{'class': 'kitty', 'adapters': {'kitty': {'tabs': [{'panes': [{'cwd': str(tmp_path), 'codex_session': {'session_id': IDENT, 'source': 'explicit_resume'}}]}]}}}]}
    number = manager.save(content)
    assert saved_binding(content, 0, 0, 0) == (IDENT, str(tmp_path))
    for args in [(-1, 0, 0), (0, -1, 0), (0, 0, -1), (0, 0, 1)]:
        with pytest.raises(CodexCheckError): saved_binding(content, *args)
    import phoenix.adapters.codex_binding as binding
    called = []
    monkeypatch.setattr(binding, 'resume_in_current_pane', lambda ident, cwd: called.append((ident, cwd)) or 0)
    with manager.connect() as db: before = db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0]
    assert main(['codex-restore', str(number), '0', '0', '0']) == 0
    assert called == [(IDENT, str(tmp_path))]
    with manager.connect() as db: assert db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0] == before
    assert main(['codex-restore', str(number), '-1', '0', '0']) == 1
    assert len(called) == 1
