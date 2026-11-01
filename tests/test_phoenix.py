import hashlib
import json
from pathlib import Path
import sqlite3
import pytest
from phoenix.core import Collector, DesktopRegistry, SnapshotManager, canonical
from phoenix.recovery import RecoveryExecutor, RecoveryPlanner, geometry
from phoenix.session import decide, decided


def window(cls='kitty', **kw):
    return {'class': cls, 'initial_class': cls, 'workspace': 2, 'monitor': 'A', 'floating': True, 'fullscreen': 0, 'at': [100, 100], 'size': [400, 300], **kw}


def state(*windows):
    return {'schema': 1, 'windows': list(windows), 'monitors': [{'name': 'A', 'x': 0, 'y': 0, 'width': 1000, 'height': 800, 'scale': 1}], 'workspaces': []}


class FakeBackend:
    def __init__(self, clients=None):
        self.clients = clients or []; self.calls = []
    def query(self, name):
        return {'clients': list(self.clients), 'monitors': [{'name': 'A', 'x': 0, 'y': 0, 'width': 1000, 'height': 800, 'scale': 1}], 'workspaces': []}[name]
    def dispatch(self, action, argument): self.calls.append((action, argument))


class FakeRegistry:
    def __init__(self, backend=None, launch=True): self.backend = backend; self.enabled = launch; self.launches = 0
    def find(self, cls): return Path('/trusted/app.desktop') if self.enabled else None
    def launch(self, entry):
        self.launches += 1
        if self.backend:
            self.backend.clients.append({'address': f'0x{self.launches:x}', 'class': 'kitty', 'floating': True, 'fullscreen': 0})


def test_snapshot_rotation_dedup_manual_and_integrity(tmp_path):
    m = SnapshotManager(tmp_path / 'db')
    manual = m.save(state(window()), name='keep')
    for i in range(65): m.save(state(window(workspace=i+1)), automatic=True)
    assert len(m.history()) == 61
    assert m.get(manual)[0]['name'] == 'keep'
    latest = m.history()[0]['id']
    assert m.save(state(window(workspace=65)), automatic=True) == latest
    assert m.integrity() == 'ok'


def test_corrupted_latest_fallback(tmp_path):
    m = SnapshotManager(tmp_path / 'db'); old = m.save(state(window()))
    new = m.save(state(window('other')))
    with m.connect() as db: db.execute('UPDATE snapshots SET payload=? WHERE id=?', ('{}', new))
    assert m.get()[0]['id'] == old
    with pytest.raises(LookupError): m.get(new)


def test_schema_migration_and_future_rejection(tmp_path):
    path = tmp_path / 'db'; sqlite3.connect(path).close()
    m = SnapshotManager(path)
    with m.connect() as db: assert db.execute('PRAGMA user_version').fetchone()[0] == 5
    with m.connect() as db:
        db.execute('DROP TABLE workspace_captures'); db.execute('DROP TABLE snapshot_previews'); db.execute('DROP TABLE preview_images'); db.execute('ALTER TABLE snapshots DROP COLUMN pinned'); db.execute('PRAGMA user_version=1')
    m = SnapshotManager(path)
    with m.connect() as db: assert db.execute('PRAGMA user_version').fetchone()[0] == 5
    with m.connect() as db: db.execute('PRAGMA user_version=999')
    with pytest.raises(ValueError): SnapshotManager(path)


def test_matching_duplicate_windows_and_repeat(tmp_path):
    m = SnapshotManager(tmp_path / 'db'); s = state(window(), window()); sid = m.save(s)
    backend = FakeBackend(); registry = FakeRegistry(backend); ex = RecoveryExecutor(backend, registry, m, .1)
    assert [r['status'] for r in ex.restore(sid, s)] == ['partial', 'partial']
    assert registry.launches == 2
    # Existing same-class Kitty windows cannot be identified reliably.
    assert [r['status'] for r in ex.restore(sid, s)] == ['uncertain', 'uncertain']
    assert registry.launches == 2


def test_timeout_prevents_duplicate_retry_and_is_durable(tmp_path):
    m = SnapshotManager(tmp_path / 'db'); s = state(window()); sid = m.save(s)
    b = FakeBackend(); registry = FakeRegistry(); ex = RecoveryExecutor(b, registry, m, .01)
    assert ex.restore(sid, s)[0]['status'] == 'timeout'
    assert ex.restore(sid, s)[0]['status'] == 'uncertain'
    assert ex.restore(sid, s)[0]['status'] == 'uncertain'
    assert registry.launches == 1


def test_interrupted_launch_is_not_repeated(tmp_path):
    m = SnapshotManager(tmp_path / 'db'); s = state(window()); sid = m.save(s)
    class Interrupted(FakeRegistry):
        def launch(self, entry): raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt): RecoveryExecutor(FakeBackend(), Interrupted(), m, .01).restore(sid, s)
    registry = FakeRegistry()
    assert RecoveryExecutor(FakeBackend(), registry, m, .01).restore(sid, s)[0]['status'] == 'uncertain'
    assert registry.launches == 0


def test_dry_run_no_mutation(tmp_path):
    m = SnapshotManager(tmp_path / 'db'); s = state(window()); sid = m.save(s); b = FakeBackend(); reg = FakeRegistry()
    assert RecoveryExecutor(b, reg, m).restore(sid, s, True)[0]['action'] == 'launch'
    assert not b.calls and not reg.launches
    with m.connect() as db: assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 0


def test_geometry_monitor_removed():
    w = window(at=[800, 600], size=[400, 300])
    assert geometry(w, state(w)['monitors'], [{'name': 'B', 'x': 0, 'y': 0, 'width': 500, 'height': 400, 'scale': 1}]) == (300, 250, 200, 150)


def test_partial_and_unsupported(tmp_path):
    m = SnapshotManager(tmp_path / 'db'); s = state(window(), window('unknown')); sid = m.save(s)
    b = FakeBackend([{'address': '0xaa', 'class': 'kitty', 'floating': True}]); r = RecoveryExecutor(b, FakeRegistry(launch=False), m).restore(sid, s)
    assert [x['status'] for x in r] == ['partial', 'unsupported']


def test_collector_private_and_exclusions():
    b = FakeBackend([{'class': 'kitty', 'pid': -1, 'title': 'secret', 'workspace': {'id': 2}, 'monitor': 0}, {'class': 'secret'}])
    s = Collector(b, {'exclude_classes': ['secret']}).collect()
    assert len(s['windows']) == 1 and 'secret' not in canonical(s) and 'title' not in canonical(s)


def test_no_hyprland_and_missing_tools(monkeypatch):
    from phoenix.core import HyprlandBackend
    def missing(*args, **kwargs): raise FileNotFoundError('missing')
    monkeypatch.setattr('subprocess.run', missing)
    with pytest.raises(RuntimeError): HyprlandBackend().query('clients')


def test_decision_session_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path)); monkeypatch.setenv('HYPRLAND_INSTANCE_SIGNATURE', 'first')
    assert not decided(); decide('skip'); assert decided()
    monkeypatch.setenv('HYPRLAND_INSTANCE_SIGNATURE', 'second'); assert not decided()


def test_desktop_security_never_uses_snapshot_command(tmp_path):
    p = tmp_path / 'evil.desktop'; p.write_text('[Desktop Entry]\nType=Application\nExec=touch /tmp/evil\nStartupWMClass=evil\n')
    # Test-owned files must not be trusted, even if the name matches.
    if p.stat().st_uid != 0: assert DesktopRegistry([tmp_path]).find('evil') is None
    b = FakeBackend(); plan = RecoveryPlanner(FakeRegistry(launch=False)).plan(state(window('evil', command='touch /tmp/evil')), b.clients)
    assert plan[0]['action'] == 'unsupported'


def test_invalid_address_never_dispatched(tmp_path):
    m = SnapshotManager(tmp_path / 'db'); s = state(window()); sid = m.save(s)
    b = FakeBackend([{'address': '0x1,evil', 'class': 'kitty'}])
    assert RecoveryExecutor(b, FakeRegistry(), m).restore(sid, s)[0]['status'] == 'error'
    assert b.calls == []


def test_lua_dispatch_rejects_injection(monkeypatch):
    from types import SimpleNamespace
    from phoenix.core import HyprlandBackend
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout='ok', stderr='')
    monkeypatch.setattr('subprocess.run', run)
    backend = HyprlandBackend()
    backend.dispatch('movetoworkspacesilent', '2,address:0xabc')
    assert calls[0][1] == 'eval' and 'workspace="2"' in calls[0][2]
    with pytest.raises(ValueError): backend.dispatch('movetoworkspacesilent', '2);evil(),address:0xabc')
    assert len(calls) == 1


def test_explicit_desktop_trust_and_change_rejected(tmp_path, monkeypatch):
    from phoenix.core import trust_desktop
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    desktop = tmp_path / 'opera.desktop'
    desktop.write_text('[Desktop Entry]\nType=Application\nExec=opera\nStartupWMClass=Opera GX\n')
    registry = DesktopRegistry([], trusted={})
    assert registry.find('Opera GX') is None
    trust_desktop(desktop)
    registry = DesktopRegistry([])
    assert registry.find('Opera GX') == desktop
    desktop.write_text('[Desktop Entry]\nType=Application\nExec=changed\nStartupWMClass=Opera GX\n')
    assert registry.find('Opera GX') is None
    with pytest.raises(ValueError): registry.launch(desktop)


def test_collector_excludes_phoenix_itself():
    b = FakeBackend([{'class': 'org.phoenix.Session'}, {'class': 'chatgpt', 'workspace': {'id': 3}}])
    assert [w['class'] for w in Collector(b, {}).collect()['windows']] == ['chatgpt']


def test_previews_deduplicate_rotate_and_manual_survives(tmp_path):
    m = SnapshotManager(tmp_path / 'db')
    def picture(data): return [{'workspace': 1, 'monitor': 'A', 'captured': 100.0, 'data': data}]
    manual = m.save(state(window()), previews=picture(b'image'))
    first = m.save(state(window()), automatic=True, previews=picture(b'image'))
    assert m.save(state(window()), automatic=True, previews=picture(b'image')) == first
    second = m.save(state(window()), automatic=True, previews=picture(b'other'))
    assert second != first
    for i in range(62): m.save(state(window(workspace=i + 1)), automatic=True, previews=picture(str(i).encode()))
    assert m.previews(manual)[0]['data'] == b'image'
    assert len(m.history()) == 61
    with m.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM preview_images WHERE digest NOT IN (SELECT digest FROM snapshot_previews)').fetchone()[0] == 0


def test_capture_skips_excluded_and_locked(monkeypatch):
    from phoenix.capture import capture_previews
    class CaptureBackend:
        def query(self, name):
            if name == 'monitors': return [{'id': 0, 'name': 'A', 'activeWorkspace': {'id': 1}}]
            return [{'monitor': 0, 'visible': True, 'class': 'Private', 'workspace': {'id': 1}}]
    def prohibited(*args, **kwargs): raise AssertionError('screen capture should not run')
    monkeypatch.setattr('phoenix.capture.subprocess.run', prohibited)
    monkeypatch.setattr('phoenix.capture.session_unlocked', lambda: False)
    assert capture_previews(CaptureBackend(), {}) == []
    monkeypatch.setattr('phoenix.capture.session_unlocked', lambda: True)
    assert capture_previews(CaptureBackend(), {'exclude_classes': ['private']}) == []
    assert capture_previews(CaptureBackend(), {'capture_previews': False}) == []


def test_previews_can_show_latest_previous_without_future(tmp_path):
    m = SnapshotManager(tmp_path / 'db')
    pic = lambda ws, data: {'workspace': ws, 'monitor': 'A', 'captured': 100.0, 'data': data}
    one = m.save(state(window()), previews=[pic(1, b'one')])
    two = m.save(state(window()), previews=[pic(2, b'two')])
    three = m.save(state(window()), previews=[pic(1, b'new')])
    assert [(p['workspace'], p['data']) for p in m.previews(two)] == [(2, b'two')]
    previous = m.previews(two, include_previous=True)
    assert {(p['workspace'], p['snapshot_id'], p['data']) for p in previous} == {(1, one, b'one'), (2, two, b'two')}
    assert {p['data'] for p in m.previews(three, include_previous=True)} == {b'new', b'two'}


def test_workspace_carousel_uses_only_selected_snapshot():
    from phoenix.timeline import Timeline, workspace_items
    selected = state(window('Editor', workspace=1), window('Browser', workspace=3))
    selected['workspaces'] = [{'id': 1}, {'id': 2}, {'id': 3}]
    previews = [{'snapshot_id': 12, 'workspace': 3, 'monitor': 'A', 'captured': 123, 'data': b'example'}]
    items = workspace_items(12, selected, previews)
    assert [x['workspace'] for x in items] == [1, 2, 3]
    assert [x['has_image'] for x in items] == [False, False, True]
    assert [x['snapshot_id'] for x in items] == [12, 12, 12]
    timeline = Timeline(items)
    timeline.step(1); timeline.step(1)
    assert timeline.current['workspace'] == 3 and timeline.current['snapshot_id'] == 12
    assert timeline.current['windows'][0]['class'] == 'Browser'


def test_workspace_capture_retention_and_history(tmp_path):
    m = SnapshotManager(tmp_path / 'db')
    first = {'workspace': 1, 'monitor': 'A', 'captured': 100.0, 'data': b'first'}
    later = {'workspace': 1, 'monitor': 'A', 'captured': 200.0, 'data': b'later'}
    m.record_workspace_captures([first, later])
    old = m.earlier_workspace_captures(150.0)
    assert len(old) == 1 and m.capture_image(old[0]['id']) == b'first'
    assert m.capture_image(m.earlier_workspace_captures(250.0)[0]['id']) == b'later'
    assert m.earlier_workspace_captures(99.0) == []
    m.save(state(window()), automatic=True)
    assert m.capture_image(old[0]['id']) == b'first'
    for i in range(241):
        m.record_workspace_captures([{'workspace': 2, 'monitor': 'A', 'captured': 300.0 + i, 'data': str(i).encode()}])
    with m.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM workspace_captures').fetchone()[0] == 240
        assert db.execute('SELECT COUNT(*) FROM preview_images WHERE digest NOT IN (SELECT digest FROM snapshot_previews UNION SELECT digest FROM workspace_captures)').fetchone()[0] == 0


def test_earlier_capture_is_explicit_and_exact_takes_priority():
    from phoenix.timeline import workspace_items
    s = state(window(workspace=1))
    earlier = [{'id': 9, 'workspace': 1, 'monitor': 'A', 'captured': 10.0}]
    item = workspace_items(12, s, [], earlier)[0]
    assert item['has_image'] and item['is_earlier'] and item['capture_id'] == 9
    exact = [{'workspace': 1, 'monitor': 'A', 'captured': 20.0}]
    item = workspace_items(12, s, exact, earlier)[0]
    assert item['has_image'] and not item['is_earlier'] and item['capture_id'] is None


def test_workspace_event_watcher(tmp_path):
    import socket
    import threading
    from phoenix.watch import watch_workspace_events
    path = tmp_path / 'socket'
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path)); listener.listen(1); listener.settimeout(3)
    stop, seen = threading.Event(), threading.Event()
    captures = []
    def capture():
        captures.append(1); seen.set()
    worker = threading.Thread(target=watch_workspace_events, args=(stop, capture), kwargs={'path': path, 'debounce': .02, 'min_interval': 0, 'reconnect': .01})
    worker.start()
    conn, _ = listener.accept()
    try:
        conn.sendall(b'activewindow>>private,do not log\nworkspacev2>>2,2\n')
        assert seen.wait(2) and len(captures) == 1
    finally:
        stop.set(); conn.close(); listener.close(); worker.join(2)
    assert not worker.is_alive()


def test_schema_two_to_three_preserves_existing_previews(tmp_path):
    path = tmp_path / 'db'
    m = SnapshotManager(path)
    sid = m.save(state(window()), previews=[{'workspace': 1, 'monitor': 'A', 'captured': 10.0, 'data': b'image'}])
    with m.connect() as db:
        db.execute('DROP TABLE workspace_captures')
        db.execute('ALTER TABLE snapshots DROP COLUMN pinned')
        db.execute('PRAGMA user_version=2')
    migrated = SnapshotManager(path)
    assert migrated.previews(sid)[0]['data'] == b'image'
    with migrated.connect() as db: assert db.execute('PRAGMA user_version').fetchone()[0] == 5


def test_earlier_capture_can_reuse_periodic_snapshot_image(tmp_path):
    m = SnapshotManager(tmp_path / 'db')
    sid = m.save(state(window()), previews=[{'workspace': 1, 'monitor': 'A', 'captured': 10.0, 'data': b'periodic'}])
    result = m.earlier_workspace_captures(11.0)[0]
    assert result['source'] == 'snapshot' and result['id'] == sid
    assert m.capture_image(result['id'], result['source'], result['workspace'], result['monitor']) == b'periodic'
    assert m.earlier_workspace_captures(9.0) == []


def test_smoothstep_animation_has_stationary_endpoints():
    from phoenix.animation import smoothstep
    assert smoothstep(-1) == 0
    assert smoothstep(0) == 0 and smoothstep(1) == 1
    assert smoothstep(2) == 1 and smoothstep(.5) == .5
    assert smoothstep(.01) < .001 and 1 - smoothstep(.99) < .001
    samples = [smoothstep(i / 100) for i in range(101)]
    assert samples == sorted(samples)


def test_pinned_snapshot_survives_rotation_and_delete(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    from phoenix.core import update_config
    update_config({'snapshot_retention': 2})
    m = SnapshotManager(tmp_path / 'db')
    pinned = m.save(state(window(workspace=1)), automatic=True)
    m.set_pinned(pinned, True)
    for i in range(2, 7): m.save(state(window(workspace=i)), automatic=True)
    assert len(m.history()) == 3 and m.get(pinned)[0]['pinned'] == 1
    m.delete(pinned)
    with pytest.raises(LookupError): m.get(pinned)


def test_backup_is_consistent_private_and_retained(tmp_path):
    m = SnapshotManager(tmp_path / 'db')
    sid = m.save(state(window()))
    path = m.backup()
    assert path.stat().st_mode & 0o077 == 0
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT id FROM snapshots').fetchone()[0] == sid
    m.backup()
    assert (path.parent / 'snapshots-1.sqlite3').exists()
    assert not m.backup_if_due()


def test_settings_preserve_unrelated_trust(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    from phoenix.core import update_config, config
    update_config({'snapshot_retention': 12, 'capture_previews': False})
    assert config()['snapshot_retention'] == 12 and not config()['capture_previews']
    with pytest.raises(ValueError): update_config({'snapshot_retention': 0})


def test_kitty_restores_only_validated_shell_cwd(tmp_path):
    m = SnapshotManager(tmp_path / 'db'); s = state(window(cwd=str(tmp_path), cwd_source='single_local_shell')); sid = m.save(s)
    b = FakeBackend()
    class Registry(FakeRegistry):
        def __init__(self, backend): super().__init__(backend); self.cwd = None
        def launch_kitty(self, entry, cwd):
            self.cwd = cwd; self.launch(entry); return True
    registry = Registry(b)
    result = RecoveryExecutor(b, registry, m, .1).restore(sid, s)
    assert registry.cwd == str(tmp_path) and result[0]['terminal_cwd'] == str(tmp_path)


def test_kitty_without_verified_source_ignores_saved_command(tmp_path):
    m = SnapshotManager(tmp_path / 'db'); s = state(window(cwd='/tmp', command='danger')); sid = m.save(s)
    b = FakeBackend()
    class Registry(FakeRegistry):
        def launch_kitty(self, entry, cwd): raise AssertionError('unverified cwd')
    registry = Registry(b)
    RecoveryExecutor(b, registry, m, .1).restore(sid, s)
    assert registry.launches == 1


def test_shell_cwd_from_single_real_child(tmp_path):
    import os
    import signal
    import subprocess
    import sys
    import time
    from phoenix.core import shell_cwd
    program = "import subprocess,time; subprocess.Popen(['bash','--noprofile','--norc','-i'],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); time.sleep(10)"
    parent = subprocess.Popen([sys.executable, '-c', program], cwd=tmp_path, start_new_session=True)
    try:
        deadline = time.monotonic() + 3
        cwd = None
        while time.monotonic() < deadline and cwd is None:
            cwd = shell_cwd(parent.pid)
            if cwd is None: time.sleep(.05)
        assert cwd == str(tmp_path)
    finally:
        os.killpg(parent.pid, signal.SIGTERM)
        parent.wait(timeout=3)
