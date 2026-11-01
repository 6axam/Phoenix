from phoenix.adapters.kitty import KittyAdapter
from phoenix.adapters.base import AdapterResult, Status
from phoenix.core import Collector
from test_phoenix import FakeBackend


def data(cwd):
    return {'version':1,'active_tab':0,'tabs':[{'layout':'splits','active_pane':0,'panes':[{'cwd':cwd,'process':'unknown'}]}]}


def test_two_independent_kitty_processes_map_by_socket_peer(monkeypatch):
    monkeypatch.setattr(KittyAdapter,'peer_pid',lambda self: {'/first':101,'/second':202}[self.settings['kitty_socket']])
    def collect(self,*args,**kwargs):
        pid=kwargs['expected_pid']
        return AdapterResult(Status.PARTIAL,data('/tmp/a' if pid==101 else '/tmp/b')),[{'tab':0,'pane':0,'data':str(pid).encode()}]
    monkeypatch.setattr(KittyAdapter,'collect',collect)
    clients=[{'class':'kitty','pid':202,'address':'0xbb','workspace':{'id':2}}, {'class':'kitty','pid':101,'address':'0xaa','workspace':{'id':1}}]
    collector=Collector(FakeBackend(clients),{'kitty_enabled':True,'kitty_scrollback':True,'kitty_sockets':['/first','/second']})
    state=collector.collect()
    mapped={w['workspace']:w['adapters']['kitty']['tabs'][0]['panes'][0]['cwd'] for w in state['windows']}
    assert mapped == {1:'/tmp/a',2:'/tmp/b'}
    for archive in collector.archives:
        assert state['windows'][archive['window']]['workspace'] == (1 if archive['data']==b'101' else 2)


def test_ambiguous_process_is_skipped_but_other_is_collected(monkeypatch):
    monkeypatch.setattr(KittyAdapter,'peer_pid',lambda self: {'/ambiguous':101,'/good':202}[self.settings['kitty_socket']])
    monkeypatch.setattr(KittyAdapter,'collect',lambda self,*args,**kwargs:(AdapterResult(Status.PARTIAL,data('/tmp/good')),[]))
    clients=[{'class':'kitty','pid':101,'address':'0xaa','workspace':{'id':1}}, {'class':'kitty','pid':101,'address':'0xab','workspace':{'id':2}}, {'class':'kitty','pid':202,'address':'0xbb','workspace':{'id':3}}]
    collector=Collector(FakeBackend(clients),{'kitty_enabled':True,'kitty_sockets':['/ambiguous','/good']})
    state=collector.collect()
    assert [w['workspace'] for w in state['windows'] if 'adapters' in w] == [3]
    assert any('ambiguous' in w for w in collector.adapter_warnings)


def test_window_replaced_during_collection_is_not_attached(monkeypatch):
    monkeypatch.setattr(KittyAdapter,'peer_pid',lambda self: 101)
    monkeypatch.setattr(KittyAdapter,'collect',lambda self,*a,**k:(AdapterResult(Status.PARTIAL,data('/tmp')),[]))
    class Changed(FakeBackend):
        def __init__(self): super().__init__(); self.calls=0
        def query(self,name):
            if name=='clients':
                self.calls+=1
                return [{'class':'kitty','pid':101,'address':'0xaa' if self.calls==1 else '0xab','workspace':{'id':1}}]
            return super().query(name)
    collector=Collector(Changed(),{'kitty_enabled':True,'kitty_socket':'/test'})
    assert 'adapters' not in collector.collect()['windows'][0]


def test_existing_kitty_windows_are_not_randomly_modified(tmp_path):
    from phoenix.core import SnapshotManager
    from phoenix.recovery import RecoveryExecutor, RecoveryPlanner
    from test_phoenix import FakeRegistry, window, state
    snapshot=state(window(workspace=1),window(workspace=2)); m=SnapshotManager(tmp_path/'db'); sid=m.save(snapshot)
    b=FakeBackend([{'class':'kitty','address':'0xaa'},{'class':'kitty','address':'0xbb'}]); reg=FakeRegistry()
    assert [p['action'] for p in RecoveryPlanner(reg).plan(snapshot,b.clients)] == ['uncertain','uncertain']
    assert [r['status'] for r in RecoveryExecutor(b,reg,m,.01).restore(sid,snapshot)] == ['uncertain','uncertain']
    assert not b.calls and reg.launches == 0


def test_new_kitty_pid_is_used_only_for_immediate_matching(tmp_path):
    from phoenix.core import SnapshotManager
    from phoenix.recovery import RecoveryExecutor
    from test_phoenix import FakeRegistry, window, state
    snapshot=state(window(workspace=1),window(workspace=2)); m=SnapshotManager(tmp_path/'db'); sid=m.save(snapshot); b=FakeBackend()
    class Registry(FakeRegistry):
        def launch_kitty_plain(self,entry):
            self.launches+=1; pid=300+self.launches
            if self.launches==1: b.clients.append({'class':'kitty','address':'0xeee','pid':999})
            b.clients.append({'class':'kitty','address':hex(pid),'pid':pid})
            return pid
    reg=Registry()
    result=RecoveryExecutor(b,reg,m,.02).restore(sid,snapshot)
    assert [x['status'] for x in result]==['partial','partial']
    assert reg.launches==2
    assert not any('address:0xeee' in argument for _,argument in b.calls)
    # A repeat refuses to guess among the existing windows.
    assert all(x['status']=='uncertain' for x in RecoveryExecutor(b,reg,m,.02).restore(sid,snapshot))
    assert reg.launches==2


def test_standard_and_mixed_split_layouts_do_not_claim_exact_geometry():
    for layout in ('horizontal','vertical','splits'):
        snapshot={'version':1,'active_tab':0,'tabs':[{'layout':layout,'active_pane':0,'panes':[{'cwd':None,'process':'unknown'} for _ in range(3)]}]}
        adapter=KittyAdapter({})
        session=adapter.session(snapshot)
        assert 'layout '+layout in session and session.count('launch')==3
        assert next(s for s in adapter.plan(snapshot) if s.id=='tab:0').status=='partial'


def test_kitty_socket_can_use_first_configured_extra(tmp_path):
    import socket
    directory=tmp_path/'private'; directory.mkdir(mode=0o700)
    path=directory/'kitty.sock'
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(path))
        adapter=KittyAdapter({'kitty_enabled':True,'kitty_sockets':[str(path)]})
        assert adapter.socket()==str(path)


def test_query_failure_after_launch_is_uncertain_and_not_retried(tmp_path):
    from phoenix.core import SnapshotManager
    from phoenix.recovery import RecoveryExecutor
    from test_phoenix import FakeRegistry, window, state
    snapshot=state(window()); m=SnapshotManager(tmp_path/'db'); sid=m.save(snapshot)
    class Backend(FakeBackend):
        def __init__(self): super().__init__(); self.calls_count=0
        def query(self,name):
            if name=='clients':
                self.calls_count+=1
                if self.calls_count==2: raise RuntimeError('disconnected')
            return super().query(name)
    b=Backend(); reg=FakeRegistry()
    first=RecoveryExecutor(b,reg,m,.01).restore(sid,snapshot)
    assert first[0]['status']=='uncertain' and reg.launches==1
    second=RecoveryExecutor(b,reg,m,.01).restore(sid,snapshot)
    assert second[0]['status']=='uncertain' and reg.launches==1


def test_adapter_diagnostics_checks_additional_sockets(tmp_path,monkeypatch):
    import socket
    import phoenix.adapters.kitty as module
    directory=tmp_path/'private'; directory.mkdir(mode=0o700); path=directory/'kitty.sock'
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(path))
        monkeypatch.setattr(module.shutil,'which',lambda name:'/bin/true')
        result=KittyAdapter({'kitty_enabled':True,'kitty_socket':'/missing','kitty_sockets':[str(path)]}).availability()
        assert result.status.value=='partial' and '1/2' in result.warnings[0]
