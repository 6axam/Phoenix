"""Real Hyprland recovery constrained to temporary Kitty child processes."""
import os
from pathlib import Path
import shutil
import time
import pytest
from phoenix.adapters.kitty import KittyAdapter
from phoenix.core import Collector, HyprlandBackend, SnapshotManager
from phoenix.recovery import RecoveryExecutor
from test_kitty_integration import kitty_factory

pytestmark = pytest.mark.skipif(os.environ.get('PHOENIX_KITTY_INTEGRATION') != '1' or not os.environ.get('HYPRLAND_INSTANCE_SIGNATURE') or not shutil.which('kitty'), reason='requires explicit opt-in and live Hyprland/Kitty')


def test_real_isolated_recovery_multiple_windows_cwds_and_repeat(tmp_path,monkeypatch,kitty_factory):
    for key in ('DATA','CONFIG','STATE'):
        monkeypatch.setenv('XDG_'+key+'_HOME',str(tmp_path/key.lower()))
    paths=[tmp_path/'project-a',tmp_path/'project-b',tmp_path/'project-c']
    for p in paths: p.mkdir()
    first,socket1=kitty_factory('first',f'new_tab One\nlayout horizontal\nlaunch --cwd={paths[0]}\nlaunch --cwd={paths[1]}\nnew_tab Two\nlayout vertical\nlaunch --cwd={paths[2]}\n')
    second,socket2=kitty_factory('second',f'new_tab Three\nlayout splits\nlaunch --cwd={paths[0]}\nlaunch --location=vsplit --cwd={paths[2]}\n')
    owned={first.pid,second.pid}; backend=HyprlandBackend()
    class IsolatedBackend:
        def query(self,name):
            result=backend.query(name)
            return [c for c in result if c.get('pid') in owned] if name=='clients' else result
        def dispatch(self,action,argument):
            address=argument.split('address:')[-1]
            assert any(c.get('address')=='0x'+address.removeprefix('0x') and c.get('pid') in owned for c in backend.query('clients'))
            return backend.dispatch(action,argument)
    isolated=IsolatedBackend(); deadline=time.monotonic()+5
    while len(isolated.query('clients'))<2 and time.monotonic()<deadline: time.sleep(.05)
    assert len(isolated.query('clients'))==2
    collector=Collector(isolated,{'kitty_enabled':True,'kitty_scrollback':True,'kitty_sockets':[socket1,socket2]})
    snapshot=collector.collect(); assert len(snapshot['windows'])==2
    assert all('kitty' in w.get('adapters',{}) for w in snapshot['windows'])
    manager=SnapshotManager(); sid=manager.save(snapshot,archives=collector.archives)
    assert len(manager.archives(sid))==5
    assert all(manager.archive_text(sid,a['window_index'],a['tab_index'],a['pane_index']) for a in manager.archives(sid))
    for p in (first,second): p.terminate(); p.wait(timeout=3)
    deadline=time.monotonic()+5
    while isolated.query('clients') and time.monotonic()<deadline: time.sleep(.05)
    assert isolated.query('clients')==[]
    class Registry:
        def __init__(self): self.started=[]
        def find(self,cls): return Path('/usr/share/applications/kitty.desktop')
        def launch_kitty_session(self,entry,session):
            p,s=kitty_factory('restore'+str(len(self.started)),session)
            owned.add(p.pid); self.started.append((p,s)); return p.pid
    registry=Registry(); executor=RecoveryExecutor(isolated,registry,manager,timeout=5)
    result=executor.restore(sid,snapshot)
    assert [x['status'] for x in result]==['partial','partial'] and len(registry.started)==2
    reconstructed=[]
    for p,s in registry.started:
        outcome,_=KittyAdapter({'kitty_enabled':True,'kitty_socket':s}).collect(expected_pid=p.pid)
        assert outcome.data
        reconstructed.append(outcome.data)
    assert sorted(sorted(pane['cwd'] for tab in w['tabs'] for pane in tab['panes']) for w in reconstructed)==sorted(sorted(pane['cwd'] for tab in w['adapters']['kitty']['tabs'] for pane in tab['panes']) for w in snapshot['windows'])
    repeated=executor.restore(sid,snapshot)
    assert [x['status'] for x in repeated]==['uncertain','uncertain'] and len(registry.started)==2


def test_real_kitty_launch_interrupted_is_not_duplicated(tmp_path,monkeypatch):
    import signal
    import subprocess
    import sys
    for key in ('DATA','CONFIG','STATE'): monkeypatch.setenv('XDG_'+key+'_HOME',str(tmp_path/key.lower()))
    manager=SnapshotManager()
    kitty={'version':1,'active_tab':0,'tabs':[{'layout':'vertical','active_pane':0,'panes':[{'cwd':str(tmp_path),'process':'unknown'}]}]}
    snapshot={'schema':1,'windows':[{'class':'kitty','initial_class':'kitty','workspace':1,'floating':False,'fullscreen':0,'adapters':{'kitty':kitty}}],'monitors':[],'workspaces':[]}
    sid=manager.save(snapshot); marker=tmp_path/'pid'; directory=tmp_path/'private'; directory.mkdir(mode=0o700)
    script='''import os,subprocess,sys,tempfile\nfrom pathlib import Path\nfrom phoenix.core import SnapshotManager\nfrom phoenix.recovery import RecoveryExecutor\nclass B:\n def query(self,name): return {'clients':[], 'monitors':[]}[name]\nclass R:\n def find(self,cls): return Path('/usr/share/applications/kitty.desktop')\n def launch_kitty_session(self,entry,session):\n  with tempfile.TemporaryFile() as f:\n   f.write(session.encode()); f.seek(0)\n   p=subprocess.Popen(['kitty','--config','NONE','-o','shell=/bin/bash --noprofile --norc','-o','allow_remote_control=socket-only','--listen-on','unix:'+sys.argv[3]+'/test.sock','--session','-'],stdin=f,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)\n  Path(sys.argv[2]).write_text(str(p.pid)); os._exit(79)\nm=SnapshotManager(); sid=int(sys.argv[1]); _,state=m.get(sid); RecoveryExecutor(B(),R(),m,.1).restore(sid,state)\n'''
    home=tmp_path/'home'; home.mkdir(exist_ok=True)
    environment=os.environ.copy(); environment.update({'HOME':str(home),'HISTFILE':'/dev/null'})
    proc=subprocess.run([sys.executable,'-c',script,str(sid),str(marker),str(directory)],env=environment,timeout=10)
    assert proc.returncode==79 and marker.exists()
    pid=int(marker.read_text()); backend=HyprlandBackend()
    class Isolated:
        def query(self,name):
            result=backend.query(name)
            return [c for c in result if c.get('pid')==pid] if name=='clients' else result
        def dispatch(self,*args): pass
    class NeverLaunch:
        def __init__(self): self.launches=0
        def find(self,cls): return Path('/tmp/test.desktop')
        def launch_kitty_session(self,*args): self.launches+=1; raise AssertionError('duplicate')
    try:
        deadline=time.monotonic()+5
        while not Isolated().query('clients') and time.monotonic()<deadline: time.sleep(.05)
        assert Isolated().query('clients')
        reg=NeverLaunch(); result=RecoveryExecutor(Isolated(),reg,manager,.1).restore(sid,snapshot)
        assert result[0]['status'] in ('partial','uncertain') and reg.launches==0
    finally:
        try:
            arguments=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
            if ('unix:'+str(directory/'test.sock')).encode() in arguments: os.kill(pid,signal.SIGTERM)
        except (FileNotFoundError, ProcessLookupError): pass


def test_real_scrollback_truncation_and_dedup(tmp_path,monkeypatch,kitty_factory):
    import subprocess
    for key in ('DATA','CONFIG','STATE'): monkeypatch.setenv('XDG_'+key+'_HOME',str(tmp_path/key.lower()))
    p,path=kitty_factory('scrollback','new_tab History\nlayout horizontal\nlaunch\nlaunch\n')
    command=b"printf 'PHOENIX_TEST_HISTORY_LINE\\n%.0s' $(seq 1 300)\n"
    subprocess.run(['kitten','@','--to','unix:'+path,'send-text','--match','all','--stdin'],input=command,check=True,timeout=5)
    adapter=KittyAdapter({'kitty_enabled':True,'kitty_socket':path,'kitty_scrollback_limit':1024})
    deadline=time.monotonic()+6
    while True:
        result,archives=adapter.collect(True,expected_pid=p.pid)
        if len(archives)==2 and all(a['data'].count(b'PHOENIX_TEST_HISTORY_LINE')>3 for a in archives): break
        if time.monotonic()>deadline: pytest.fail('Temporary shells did not emit test history')
        time.sleep(.1)
    assert all(len(a['data'])<=1024 for a in archives)
    assert all(pane['scrollback_capture']=='truncated' for pane in result.data['tabs'][0]['panes'])
    from phoenix.core import update_config
    update_config({'kitty_scrollback_limit':1024,'kitty_archive_total_limit':1024*1024})
    state={'schema':1,'windows':[{'class':'kitty','adapters':{'kitty':result.data}}]}
    records=[dict(a,window=0) for a in archives]
    m=SnapshotManager(); first=m.save(state,automatic=True,archives=records)
    second=m.save(state,automatic=True,archives=records)
    assert first==second and len(m.archives(first))==2
    assert all(m.archive_text(first,0,0,i) for i in (0,1))
