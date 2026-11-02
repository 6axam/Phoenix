"""Opt-in live Kitty checks. Creates and closes only its own temporary processes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import pytest
from phoenix.adapters.kitty import KittyAdapter
from phoenix.core import Collector, HyprlandBackend

pytestmark = pytest.mark.skipif(os.environ.get('PHOENIX_KITTY_INTEGRATION') != '1' or not os.environ.get('HYPRLAND_INSTANCE_SIGNATURE') or not shutil.which('kitty') or not shutil.which('kitten'), reason='requires explicit opt-in and live Hyprland/Kitty')


@pytest.fixture
def kitty_factory(tmp_path, monkeypatch):
    for key in ('DATA','CONFIG','STATE'):
        monkeypatch.setenv('XDG_'+key+'_HOME',str(tmp_path/key.lower()))
    home=tmp_path/'home'; home.mkdir(exist_ok=True)
    directory=tmp_path/'s'; directory.mkdir(mode=0o700); processes=[]
    def launch(name,session):
        sock=directory/(name+'.sock')
        environment=os.environ.copy(); environment.update({'HOME':str(home),'HISTFILE':'/dev/null'})
        p=subprocess.Popen(['kitty','--config','NONE','-o','shell=/bin/bash --noprofile --norc','-o','allow_remote_control=socket-only','--listen-on','unix:'+str(sock),'--session','-'],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True,env=environment)
        processes.append(p)
        assert p.stdin is not None
        p.stdin.write(session.encode()); p.stdin.close()
        deadline=time.monotonic()+8
        expected_panes=sum(line.strip().startswith('launch') for line in session.splitlines())
        expected_os=1+sum(line.strip()=='new_os_window' for line in session.splitlines())
        while time.monotonic()<deadline:
            if sock.exists():
                try:
                    response=subprocess.run(['kitten','@','--to','unix:'+str(sock),'ls'],capture_output=True,text=True,timeout=2,check=True)
                    windows=json.loads(response.stdout)
                    panes=sum(len(tab.get('windows',[])) for window in windows for tab in window.get('tabs',[]))
                    if len(windows)>=expected_os and panes>=expected_panes: return p,str(sock)
                except (OSError,subprocess.SubprocessError,ValueError): pass
            time.sleep(.05)
        raise RuntimeError('Temporary Kitty did not finish opening its test panels')
    yield launch
    for process in processes:
        if process.poll() is None: process.terminate()
    for process in processes:
        try: process.wait(timeout=3)
        except subprocess.TimeoutExpired: process.kill(); process.wait()


def get_tab(path):
    result=subprocess.run(['kitten','@','--to','unix:'+path,'ls'],capture_output=True,text=True,check=True,timeout=5)
    return json.loads(result.stdout)[0]['tabs'][0]


def neighbors(tab):
    windows=tab['windows']; ids={w['id']:i for i,w in enumerate(windows)}
    return [{direction:sorted(ids[n] for n in entries if n in ids) for direction,entries in w.get('neighbors',{}).items()} for w in windows]


def test_real_save_as_session_export_is_not_executed(kitty_factory, tmp_path):
    source, path = kitty_factory('export-source', 'new_tab Export\nlayout splits\nlaunch\nlaunch --location=vsplit\nlaunch --location=hsplit\n')
    target = tmp_path / 'export.kitty-session'
    subprocess.run(['kitten', '@', '--to', 'unix:' + path, 'action', 'save_as_session', f'--save-only {target}'], check=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 5
    while not target.exists() and time.monotonic() < deadline:
        time.sleep(.05)
    assert target.is_file() and target.stat().st_size < 1024 * 1024
    exported = target.read_text()
    assert 'layout splits' in exported
    assert sum(line.startswith('launch') for line in exported.splitlines()) == 3
    assert source.poll() is None


@pytest.mark.parametrize('layout',['horizontal','vertical','splits'])
def test_real_layout_restore(kitty_factory,layout):
    extra='launch --location=vsplit\nlaunch --location=hsplit\n' if layout=='splits' else 'launch\nlaunch\n'
    source,path=kitty_factory('source',f'new_tab Demo\nlayout {layout}\nlaunch\n'+extra)
    adapter=KittyAdapter({'kitty_enabled':True,'kitty_socket':path})
    result,_=adapter.collect(expected_pid=source.pid)
    assert result.data and len(result.data['tabs'][0]['panes'])==3
    restored,other=kitty_factory('restored',adapter.session(result.data))
    other_result,_=KittyAdapter({'kitty_enabled':True,'kitty_socket':other}).collect(expected_pid=restored.pid)
    assert other_result.data['tabs'][0]['layout']==layout
    assert len(other_result.data['tabs'][0]['panes'])==3
    if layout in ('horizontal','vertical'):
        assert neighbors(get_tab(path))==neighbors(get_tab(other))
    else:
        assert next(step for step in adapter.plan(result.data) if step.id=='tab:0').status=='partial'


def test_real_multiple_processes_and_ambiguous_same_process(kitty_factory):
    a,sa=kitty_factory('a','new_tab A\nlaunch\nlaunch\n')
    b,sb=kitty_factory('b','new_tab B\nlaunch\nnew_tab C\nlaunch\n')
    ambiguous,sc=kitty_factory('c','new_tab D\nlaunch\nnew_os_window\nnew_tab E\nlaunch\n')
    # Wait for Hyprland mapping, then collect each socket against the live client list.
    backend=HyprlandBackend(); deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        clients=backend.query('clients')
        if sum(c.get('pid') in (a.pid,b.pid,ambiguous.pid) for c in clients)>=4: break
        time.sleep(.05)
    collector=Collector(backend,{'kitty_enabled':True,'kitty_sockets':[sa,sb,sc]})
    snapshot=collector.collect()
    kitty=[w['adapters']['kitty'] for w in snapshot['windows'] if 'kitty' in w.get('adapters',{})]
    assert sorted(tuple(len(t['panes']) for t in k['tabs']) for k in kitty)==[(1,1),(2,)]
    assert any('ambiguous' in warning for warning in collector.adapter_warnings)
