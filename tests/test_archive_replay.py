import io
import os
import pytest
from phoenix.archive_replay import replay
from phoenix.core import SnapshotManager
from phoenix.recovery import selected_archive_refs


def make_state(tmp_path):
    pane = {'cwd': str(tmp_path), 'process': 'unknown', 'scrollback_capture': 'truncated'}
    data = {'version': 1, 'active_tab': 0, 'tabs': [{'layout': 'horizontal', 'active_pane': 0, 'panes': [pane, dict(pane)]}, {'layout': 'vertical', 'active_pane': 0, 'panes': [dict(pane)]}]}
    return {'windows': [{'class': 'kitty', 'adapters': {'kitty': data}}]}, data


def test_replay_checks_integrity_sanitizes_and_marks_historical_content(tmp_path):
    manager = SnapshotManager(tmp_path/'db'); state, _ = make_state(tmp_path)
    snapshot = manager.save(state, archives=[{'window': 0, 'tab': 0, 'pane': 0, 'data': b'hello\x1b]52;c;secret\x07visible\x1b[31mred'}])
    out = io.StringIO(); replay(manager, snapshot, 0, 0, 0, out)
    value = out.getvalue()
    assert 'СОХРАНЁННАЯ ИСТОРИЯ' in value and 'усечена' in value and 'НОВАЯ ОБОЛОЧКА' in value
    assert 'hellovisiblered' in value and 'secret' not in value and '\x1b' not in value
    with manager.connect() as db: db.execute('UPDATE archive_chunks SET data=?', (b'broken',))
    out = io.StringIO()
    with pytest.raises(ValueError): replay(manager, snapshot, 0, 0, 0, out)
    assert out.getvalue() == ''


def test_replay_rejects_negative_wrong_window_and_missing_archive(tmp_path):
    manager = SnapshotManager(tmp_path/'db'); state, _ = make_state(tmp_path)
    snap = manager.save(state)
    for coords in [(snap, -1, 0, 0), (snap, 0, -1, 0), (snap, 0, 0, -1)]:
        with pytest.raises(ValueError): replay(manager, *coords, io.StringIO())
    with pytest.raises(LookupError): replay(manager, snap, 0, 0, 0, io.StringIO())


def test_selected_panes_map_to_original_archives(tmp_path):
    _, data = make_state(tmp_path)
    stored = {(2, 0, 0), (2, 0, 1), (2, 1, 0)}
    result = selected_archive_refs(9, 2, data, {(0, 1), (1, 0)}, stored)
    assert result == {(0, 0): (9, 2, 0, 1), (1, 0): (9, 2, 1, 0)}


def test_history_replay_requires_explicit_selection(tmp_path):
    from phoenix.adapters.kitty import KittyAdapter, KittyError
    _, data = make_state(tmp_path)
    adapter = KittyAdapter({})
    assert 'archive_bootstrap.py' not in adapter.session(data)
    with pytest.raises(KittyError):
        adapter.session(data, archive_refs={(0, 0): (1, 0, 0, '1; touch /tmp/bad')})


def test_restore_only_replays_selected_pane_and_does_not_repeat(tmp_path):
    from phoenix.recovery import RecoveryExecutor
    from test_phoenix import FakeBackend, FakeRegistry, window, state
    _, kitty = make_state(tmp_path)
    snap = state(window(adapters={'kitty': kitty}))
    manager = SnapshotManager(tmp_path/'db')
    sid = manager.save(snap, archives=[{'window': 0, 'tab': 0, 'pane': 0, 'data': b'first'}, {'window': 0, 'tab': 0, 'pane': 1, 'data': b'second'}])
    backend = FakeBackend()
    class Registry(FakeRegistry):
        def __init__(self): super().__init__(backend); self.sessions = []
        def launch_kitty_session(self, entry, session):
            self.sessions.append(session); self.launch(entry); return True
    registry = Registry(); executor = RecoveryExecutor(backend, registry, manager, .1)
    plan = executor.restore(sid, snap, dry_run=True, components={0: {(0, 1)}}, restore_history=True)
    assert next(s for s in plan[0]['steps'] if s['id'] == 'archive:0:1')['detail'].startswith('Historical text')
    result = executor.restore(sid, snap, components={0: {(0, 1)}}, restore_history=True)
    assert result[0]['terminal_history_requested'] == 1
    assert len(registry.sessions) == 1
    assert f' {sid} 0 0 1' in registry.sessions[0] and f' {sid} 0 0 0' not in registry.sessions[0]
    executor.restore(sid, snap, components={0: {(0, 1)}}, restore_history=True)
    assert len(registry.sessions) == 1
