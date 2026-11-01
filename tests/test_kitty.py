import json
import os
import socket
from pathlib import Path
import pytest
from phoenix.adapters import AdapterRegistry, KittyAdapter, builtin_adapters
from phoenix.adapters.kitty import KittyError, bounded_command
from phoenix.core import Collector, SnapshotManager, canonical, update_config
from phoenix.recovery import RecoveryExecutor, RecoveryPlanner
from phoenix.text import safe_text


@pytest.fixture
def kitty_socket(tmp_path):
    directory = tmp_path / 'private'; directory.mkdir(mode=0o700)
    sock = socket.socket(socket.AF_UNIX); path = directory / 'kitty.sock'; sock.bind(str(path)); sock.listen(1)
    yield path
    sock.close()


def payload():
    return [{'id': 1, 'tabs': [
        {'is_active': True, 'layout': 'horizontal', 'windows': [
            {'id': 11, 'cwd': '/tmp', 'is_active': False, 'cmdline': ['secret-command'], 'env': {'SECRET': 'token'}, 'foreground_processes': [{'cmdline': ['secret']}]},
            {'id': 12, 'cwd': '/tmp', 'is_active': True, 'cmdline': ['private']}]},
        {'is_active': False, 'layout': 'splits', 'windows': [{'id': 13, 'cwd': '/tmp', 'is_active': True}]}]}]


def test_registry_and_safe_collection(kitty_socket):
    calls=[]
    def runner(args, limit, timeout):
        calls.append(args)
        return json.dumps(payload()).encode() if args[-1] == 'ls' else (b'sensitive password' if 'id:12' in args else b'screen history')
    settings={'kitty_enabled': True, 'kitty_socket': str(kitty_socket), 'kitty_exclude_panels': [13], 'kitty_exclude_text': ['password']}
    registry=builtin_adapters(settings); adapter=registry.for_window({'class':'kitty'})
    assert adapter.name == 'kitty' and registry.for_window({'class':'Other'}).name == 'generic'
    assert registry.for_window({'class':'Other'}).collect()[0].status.value == 'unsupported'
    duplicate=AdapterRegistry(); duplicate.register(adapter)
    with pytest.raises(ValueError): duplicate.register(adapter)
    result, archives=KittyAdapter(settings, runner).collect(True)
    assert result.status.value == 'partial' and len(result.data['tabs']) == 2
    assert [len(t['panes']) for t in result.data['tabs']] == [2,1]
    assert result.data['tabs'][0]['active_pane'] == 1 and result.data['active_tab'] == 0
    assert 'secret' not in canonical(result.data) and 'token' not in canonical(result.data)
    assert [(x['tab'],x['pane']) for x in archives] == [(0,0)]
    assert len(calls) == 3 and any('content rule' in w for w in result.warnings)


def test_kitty_unavailable_and_untrusted_socket(tmp_path, kitty_socket):
    assert KittyAdapter({}).availability().status.value == 'skipped'
    assert KittyAdapter({'kitty_enabled':True, 'kitty_socket':str(tmp_path/'missing')}).availability().status.value == 'unsupported'
    kitty_socket.parent.chmod(0o755)
    with pytest.raises(KittyError): KittyAdapter({'kitty_socket':str(kitty_socket)}).socket()


def test_session_cannot_restore_commands_or_inject_syntax(kitty_socket, tmp_path):
    adapter=KittyAdapter({'kitty_socket':str(kitty_socket)})
    evil = tmp_path / 'x$HOME'; evil.mkdir()
    data={'version':1,'active_tab':0,'tabs':[{'layout':'splits','active_pane':1,'panes':[{'cwd':str(tmp_path),'process':'other','command':'touch /tmp/bad'}, {'cwd':str(evil),'process':'unknown'}]}]}
    session=adapter.session(data)
    assert 'touch' not in session and '$HOME' not in session and 'launch\n' in session
    assert session.count('launch') == 2 and 'focus_tab 0' in session
    steps=adapter.plan(data)
    assert any(s.requires_confirmation and s.status == 'unavailable' for s in steps)
    data['tabs'][0]['layout'] = 'splits\nlaunch evil'
    with pytest.raises(KittyError): adapter.session(data)


def test_bounded_command_limits_and_timeout():
    import sys
    with pytest.raises(KittyError): bounded_command([sys.executable,'-c','print("x"*10000)'],100,.8)
    with pytest.raises(KittyError): bounded_command([sys.executable,'-c','import time; time.sleep(5)'],100,.1)


def test_archives_dedup_rotation_purge_and_corruption(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME',str(tmp_path/'config'))
    m=SnapshotManager(tmp_path/'db'); content=b'hello' * 60000
    state={'schema':1,'windows':[{'class':'kitty'}]}
    record={'window':0,'tab':0,'pane':0,'data':content}
    a=m.save(state,archives=[record]); b=m.save(state,archives=[record])
    assert m.archive_text(a,0,0,0)==content and m.archive_text(b,0,0,0)==content
    with m.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM archive_chunks').fetchone()[0] == 2
        assert db.execute('SELECT COUNT(*) FROM archive_refs').fetchone()[0] == 4
        db.execute('UPDATE archive_chunks SET data=? WHERE digest=(SELECT digest FROM archive_refs WHERE snapshot_id=? LIMIT 1)',(b'corrupt',a))
    with pytest.raises(ValueError): m.archive_text(a,0,0,0)
    m.delete(a); m.purge_archives()
    assert m.archives(b)==[]
    assert m.get(b)[1] == state


def test_archive_quota_skips_without_fake_success(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME',str(tmp_path/'config'))
    update_config({'kitty_archive_total_limit':1024,'kitty_scrollback_limit':2048})
    m=SnapshotManager(tmp_path/'db'); sid=m.save({'windows':[]}, archives=[{'window':0,'tab':0,'pane':0,'data':os.urandom(1500)}])
    assert m.archives(sid)==[] and m.last_archive_warnings


def test_archive_view_does_not_emit_terminal_controls():
    text=safe_text(b'ok\x1b[31mred\x1b[0m\x1b]52;c;secret\x07done\n')
    assert '\x1b' not in text and 'secret' not in text and 'okred' in text


def test_kitty_plan_and_idempotent_restore(tmp_path):
    from test_phoenix import FakeBackend, FakeRegistry, state, window
    data={'version':1,'active_tab':0,'tabs':[{'layout':'tall','active_pane':0,'panes':[{'cwd':str(tmp_path),'process':'other'}]}]}
    snap=state(window(adapters={'kitty':data})); m=SnapshotManager(tmp_path/'db'); sid=m.save(snap)
    b=FakeBackend()
    class Registry(FakeRegistry):
        def __init__(self, backend): super().__init__(backend); self.sessions=[]
        def launch_kitty_session(self, entry, session): self.sessions.append(session); self.launch(entry); return True
    registry=Registry(b); ex=RecoveryExecutor(b,registry,m,.05)
    plan=RecoveryPlanner(registry).plan(snap,[])
    assert any(s['requires_confirmation'] and s['status']=='unavailable' for s in plan[0]['steps'])
    assert ex.restore(sid,snap)[0]['kitty_structure']=='recreated_best_effort'
    ex.restore(sid,snap)
    assert len(registry.sessions)==1 and 'launch' in registry.sessions[0]


def test_auto_snapshot_dedup_includes_archived_content(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME',str(tmp_path/'config'))
    m=SnapshotManager(tmp_path/'db'); state={'schema':1,'windows':[{'class':'kitty'}]}
    archive={'window':0,'tab':0,'pane':0,'data':b'unchanged'}
    first=m.save(state,automatic=True,archives=[archive])
    assert m.save(state,automatic=True,archives=[archive]) == first
    changed=dict(archive,data=b'changed')
    second=m.save(state,automatic=True,archives=[changed])
    assert second != first and m.archive_text(second,0,0,0)==b'changed'


def test_missing_chunk_and_invalid_archives(tmp_path):
    m=SnapshotManager(tmp_path/'db'); state={'schema':1,'windows':[{'class':'kitty'}]}
    a=m.save(state,archives=[{'window':0,'tab':0,'pane':0,'data':b'A'*300000}])
    with m.connect() as db: db.execute('DELETE FROM archive_refs WHERE snapshot_id=? AND part=1',(a,))
    with pytest.raises(ValueError): m.archive_text(a,0,0,0)
    b=m.save(state,archives=[{'window':-1,'tab':0,'pane':0,'data':b'bad'}])
    assert m.archives(b)==[] and m.last_archive_warnings


def test_selected_kitty_components_only_and_no_duplicate(tmp_path):
    from test_phoenix import FakeBackend, FakeRegistry, state, window
    data={'version':1,'active_tab':0,'tabs':[{'layout':'tall','active_pane':0,'panes':[{'cwd':None,'process':'unknown'}, {'cwd':None,'process':'other'}]}]}
    snap=state(window(adapters={'kitty':data})); m=SnapshotManager(tmp_path/'db'); sid=m.save(snap); backend=FakeBackend()
    class Registry(FakeRegistry):
        def __init__(self,backend): super().__init__(backend); self.sessions=[]
        def launch_kitty_session(self,entry,session): self.sessions.append(session); self.launch(entry); return True
    reg=Registry(backend); executor=RecoveryExecutor(backend,reg,m,.05)
    result=executor.restore(sid,snap,components={0:set()})
    assert result[0]['status']=='skipped' and reg.launches==0
    result=executor.restore(sid,snap,components={0:{(0,1)}})
    assert result[0]['status']=='partial' and reg.sessions[0].count('launch')==1
    executor.restore(sid,snap,components={0:{(0,1)}})
    assert reg.launches==1


def test_collector_excluded_class_never_calls_kitty(monkeypatch):
    from test_phoenix import FakeBackend
    monkeypatch.setattr(KittyAdapter,'collect',lambda *a,**k: (_ for _ in ()).throw(AssertionError('must not collect')))
    backend=FakeBackend([{'class':'kitty','pid':1,'workspace':{'id':1}}])
    state=Collector(backend,{'kitty_enabled':True,'exclude_classes':['kitty']}).collect()
    assert state['windows']==[]


def test_safe_view_removes_osc_terminated_by_st():
    assert safe_text(b'hello\x1b]52;c;secret\x1b\\world') == 'helloworld'


def test_text_capture_tail_is_bounded_and_reported():
    import sys
    from phoenix.adapters.kitty import KittyTruncated
    with pytest.raises(KittyTruncated) as exc:
        bounded_command([sys.executable,'-c','import sys;sys.stdout.write("A"*2000+"END")','get-text'],1024,2)
    assert len(exc.value.data)==1024 and exc.value.data.endswith(b'END')
    assert 'END' not in str(exc.value)


def test_scrollback_is_explicit_opt_in(kitty_socket):
    calls=[]
    def runner(args,limit,timeout):
        calls.append(args)
        return json.dumps(payload()).encode()
    result, archives=KittyAdapter({'kitty_enabled':True,'kitty_socket':str(kitty_socket)},runner).collect(False)
    assert result.data and not archives and len(calls)==1 and calls[0][-1]=='ls'


def test_structured_step_journal_and_unsafe_process_never_run(tmp_path):
    from test_phoenix import FakeBackend, FakeRegistry, state, window
    data={'version':1,'active_tab':0,'tabs':[{'layout':'tall','active_pane':0,'panes':[{'cwd':None,'process':'other','command':'touch /tmp/never'}]}]}
    snapshot=state(window(adapters={'kitty':data})); m=SnapshotManager(tmp_path/'db'); sid=m.save(snapshot); b=FakeBackend()
    class Registry(FakeRegistry):
        def launch_kitty_session(self,entry,session):
            assert 'touch' not in session
            self.launch(entry); return True
    RecoveryExecutor(b,Registry(b),m,.05).restore(sid,snapshot)
    log=m.recovery_log(sid)
    assert any(r['step_id']=='process:0:0' and r['status']=='unsupported' for r in log)
    assert any(r['step_id']=='pane:0:0' and r['status']=='partial' for r in log)


def test_collector_attaches_state_and_archive_to_same_sorted_window(monkeypatch):
    from test_phoenix import FakeBackend
    from phoenix.adapters.base import AdapterResult, Status
    d={'version':1,'active_tab':0,'tabs':[{'layout':'tall','active_pane':0,'panes':[{'cwd':'/tmp','process':'unknown'}]}]}
    monkeypatch.setattr(KittyAdapter,'collect',lambda self,*a,**k:(AdapterResult(Status.PARTIAL,d),[{'tab':0,'pane':0,'data':b'content'}]))
    monkeypatch.setattr(KittyAdapter,'peer_pid',lambda self:1)
    b=FakeBackend([{'class':'kitty','pid':1,'address':'0xabc','workspace':{'id':1}},{'class':'aaa','workspace':{'id':2}}])
    c=Collector(b,{'kitty_enabled':True,'kitty_scrollback':True,'kitty_socket':'/tmp/test.sock'}); snap=c.collect()
    index=c.archives[0]['window']
    assert snap['windows'][index]['class']=='kitty' and snap['windows'][index]['adapters']['kitty']==d


def test_legacy_schema_four_migrates_without_losing_snapshot(tmp_path):
    import sqlite3
    path=tmp_path/'db'; m=SnapshotManager(path); sid=m.save({'schema':1,'windows':[{'class':'kitty'}]})
    with m.connect() as db:
        db.execute('DROP TABLE archive_refs'); db.execute('DROP TABLE archive_manifests'); db.execute('DROP TABLE archive_chunks'); db.execute('DROP TABLE recovery_steps'); db.execute('PRAGMA user_version=4')
    upgraded=SnapshotManager(path)
    assert upgraded.get(sid)[1]['windows'][0]['class']=='kitty'
    with upgraded.connect() as db: assert db.execute('PRAGMA user_version').fetchone()[0]==5


def test_unselected_uncertain_launch_remains_suppressed(tmp_path):
    from test_phoenix import FakeBackend, FakeRegistry, state, window
    m=SnapshotManager(tmp_path/'db'); s=state(window('kitty'),window('unknown')); sid=m.save(s)
    b=FakeBackend(); reg=FakeRegistry(); ex=RecoveryExecutor(b,reg,m,.01)
    assert ex.restore(sid,s,selected={0})[0]['status']=='timeout'
    assert ex.restore(sid,s,selected={1})[0]['status']=='timeout'
    count=reg.launches
    assert ex.restore(sid,s,selected={0})[0]['status']=='uncertain'
    assert reg.launches==count


def test_cli_archive_view_and_explicit_purge(tmp_path,monkeypatch,capsys):
    monkeypatch.setenv('XDG_DATA_HOME',str(tmp_path/'data'))
    monkeypatch.setenv('XDG_CONFIG_HOME',str(tmp_path/'config'))
    monkeypatch.setenv('XDG_STATE_HOME',str(tmp_path/'state'))
    from phoenix.cli import main
    m=SnapshotManager(); sid=m.save({'schema':1,'windows':[{'class':'kitty'}]},archives=[{'window':0,'tab':0,'pane':0,'data':b'visible\x1b[31mred'}])
    assert main(['archive','list',str(sid)])==0 and 'pane_index' in capsys.readouterr().out
    assert main(['archive','show',str(sid),'0','0','0'])==0
    out=capsys.readouterr().out
    assert 'visiblered' in out and '\x1b' not in out
    with pytest.raises(SystemExit): main(['archive','purge'])
    assert main(['archive','purge','--confirm'])==0 and m.archives(sid)==[]


def test_scrollback_batch_memory_is_bounded(kitty_socket, monkeypatch):
    import phoenix.adapters.kitty as module
    monkeypatch.setattr(module, 'MAX_CAPTURE_BATCH', 1500)
    def runner(args, limit, timeout):
        return json.dumps(payload()).encode() if args[-1] == 'ls' else b'x'*1000
    result, archives=KittyAdapter({'kitty_enabled':True,'kitty_socket':str(kitty_socket)},runner).collect(True)
    assert sum(len(a['data']) for a in archives) <= 1500
    assert any('memory budget' in warning for warning in result.warnings)


def test_recovery_plan_reflects_selected_panes_and_open_window(tmp_path):
    from test_phoenix import FakeBackend, FakeRegistry, state, window
    data={'version':1,'active_tab':0,'tabs':[{'layout':'tall','active_pane':0,'panes':[{'cwd':None,'process':'unknown'},{'cwd':None,'process':'other'}]}]}
    snap=state(window(adapters={'kitty':data})); m=SnapshotManager(tmp_path/'db'); sid=m.save(snap)
    backend=FakeBackend([{'address':'0xaa','class':'kitty'}]); executor=RecoveryExecutor(backend,FakeRegistry(),m)
    plan=executor.restore(sid,snap,dry_run=True,selected={0},components={0:{(0,0)}})
    statuses={step['id']:step['status'] for step in plan[0]['steps']}
    assert statuses['pane:0:0']=='unverified' and statuses['pane:0:1']=='skipped'
    assert executor.restore(sid,snap,dry_run=True,selected=set())[0]['action']=='skip'


def test_persisted_status_matches_archive_quota_and_truncation(tmp_path,monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME',str(tmp_path/'config'))
    update_config({'kitty_archive_total_limit':1024,'kitty_scrollback_limit':2048})
    def snap(status):
        return {'schema':1,'windows':[{'class':'kitty','adapters':{'kitty':{'version':1,'active_tab':0,'tabs':[{'layout':'tall','active_pane':0,'panes':[{'cwd':None,'process':'unknown','scrollback_capture':status}]}]}}}]}
    m=SnapshotManager(tmp_path/'db')
    def status(sid): return m.get(sid)[1]['windows'][0]['adapters']['kitty']['tabs'][0]['panes'][0]['scrollback_capture']
    a=m.save(snap('captured'),archives=[{'window':0,'tab':0,'pane':0,'data':os.urandom(1500)}])
    assert status(a)=='quota_exceeded' and m.archives(a)==[]
    b=m.save(snap('captured'),archives=[{'window':0,'tab':0,'pane':0,'data':b'hello'}])
    assert status(b)=='saved' and m.archive_text(b,0,0,0)==b'hello'
    c=m.save(snap('truncated'),archives=[{'window':0,'tab':0,'pane':0,'data':b'part'}])
    assert status(c)=='truncated' and m.archive_text(c,0,0,0)==b'part'
    d=m.save(snap('captured'),archives=[])
    assert status(d)=='failed' and m.archives(d)==[]


def test_archive_status_tracks_purge_without_rewriting_old_snapshot(tmp_path):
    m=SnapshotManager(tmp_path/'db')
    data={'version':1,'active_tab':0,'tabs':[{'layout':'tall','active_pane':0,'panes':[{'cwd':None,'process':'other','scrollback_capture':'captured'}]}]}
    state={'schema':1,'windows':[{'class':'kitty','adapters':{'kitty':data}}]}
    sid=m.save(state,archives=[{'window':0,'tab':0,'pane':0,'data':b'history'}])
    def status(): return m.get(sid)[1]['windows'][0]['adapters']['kitty']['tabs'][0]['panes'][0]['scrollback_capture']
    assert status()=='saved'
    m.purge_archives()
    assert status()=='not_saved'
    assert m.integrity()=='ok'


def test_full_quota_reuses_chunks_without_recompression(tmp_path,monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME',str(tmp_path/'config'))
    update_config({'kitty_archive_total_limit':2048,'kitty_scrollback_limit':2048})
    m=SnapshotManager(tmp_path/'db'); content=os.urandom(1500); state={'schema':1,'windows':[{'class':'kitty'}]}
    record={'window':0,'tab':0,'pane':0,'data':content}
    first=m.save(state,archives=[record]); update_config({'kitty_archive_total_limit':1024})
    import phoenix.core as core
    monkeypatch.setattr(core.zlib,'compress',lambda *a,**k: (_ for _ in ()).throw(AssertionError('unexpected compression')))
    second=m.save(state,archives=[record]); assert m.archive_text(second,0,0,0)==content
    third=m.save(state,archives=[dict(record,data=os.urandom(1200))]); assert m.archives(third)==[] and m.last_archive_warnings
    assert m.archive_text(first,0,0,0)==content
