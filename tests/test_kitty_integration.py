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


@pytest.mark.parametrize('locations', [
    ('vsplit',), ('hsplit',),
    ('vsplit', 'hsplit'), ('hsplit', 'vsplit'),
    ('vsplit', 'vsplit'), ('hsplit', 'hsplit'),
])
def test_real_small_split_topology_restored(kitty_factory, tmp_path, locations):
    directories = [tmp_path / ('pane' + str(i)) for i in range(len(locations) + 1)]
    for directory in directories: directory.mkdir()
    session = 'new_tab Topology\nlayout splits\nlaunch --cwd=' + str(directories[0]) + '\n'
    for i, location in enumerate(locations, 1):
        session += f'launch --location={location} --cwd={directories[i]}\n'
    source, path = kitty_factory('source-topology', session)
    adapter = KittyAdapter({'kitty_enabled': True, 'kitty_socket': path})
    result, _ = adapter.collect(expected_pid=source.pid)
    assert result.data and 'topology' in result.data['tabs'][0]
    restored, target = kitty_factory('target-topology', adapter.session(result.data))
    def by_cwd(tab):
        ws = tab['windows']; names = {w['id']: w['cwd'] for w in ws}
        return {w['cwd']: {direction: sorted(names[n] for n in w.get('neighbors', {}).get(direction, [])) for direction in ('left', 'right', 'top', 'bottom')} for w in ws}
    assert by_cwd(get_tab(path)) == by_cwd(get_tab(target))
    assert restored.poll() is None


def test_real_topology_preserves_active_panes_across_tabs(kitty_factory, tmp_path):
    directories = [tmp_path / str(i) for i in range(6)]
    for directory in directories: directory.mkdir()
    session = ''
    for offset in (0, 3):
        session += (f'new_tab Tab{offset}\nlayout splits\nlaunch --cwd={directories[offset]}\nfocus\n'
                    f'launch --location=vsplit --cwd={directories[offset+1]}\n'
                    f'launch --location=hsplit --cwd={directories[offset+2]}\n')
    session += 'focus_tab 0\n'
    source, path = kitty_factory('source-tabs', session)
    adapter = KittyAdapter({'kitty_enabled': True, 'kitty_socket': path})
    result, _ = adapter.collect(expected_pid=source.pid)
    assert result.data and all('topology' in tab for tab in result.data['tabs'])
    restored, target = kitty_factory('restored-tabs', adapter.session(result.data))
    re_collected, _ = KittyAdapter({'kitty_enabled': True, 'kitty_socket': target}).collect(expected_pid=restored.pid)
    assert re_collected.data['active_tab'] == result.data['active_tab']
    def active_cwds(data):
        return [tab['panes'][tab['active_pane']]['cwd'] for tab in data['tabs']]
    assert active_cwds(re_collected.data) == active_cwds(result.data)


def test_real_resized_splits_preserve_neighbors_not_exact_proportions(kitty_factory, tmp_path):
    dirs = [tmp_path / str(i) for i in range(3)]
    for directory in dirs: directory.mkdir()
    session = (f'new_tab Resized\nlayout splits\nlaunch --cwd={dirs[0]}\n'
               f'launch --location=vsplit --bias=25 --cwd={dirs[1]}\n'
               f'launch --location=hsplit --bias=35 --cwd={dirs[2]}\n')
    source, path = kitty_factory('source-resized', session)
    adapter = KittyAdapter({'kitty_enabled': True, 'kitty_socket': path})
    result, _ = adapter.collect(expected_pid=source.pid)
    assert result.data and 'topology' in result.data['tabs'][0]
    _, target = kitty_factory('target-resized', adapter.session(result.data))
    def edges(tab):
        names = {w['id']: w['cwd'] for w in tab['windows']}
        return {w['cwd']: {d: sorted(names[n] for n in w.get('neighbors', {}).get(d, [])) for d in ('left', 'right', 'top', 'bottom')} for w in tab['windows']}
    assert edges(get_tab(path)) == edges(get_tab(target))
    assert 'sizes may differ' in next(s.detail for s in adapter.plan(result.data) if s.id == 'tab:0')


def test_real_historical_output_appears_in_new_kitty_panel(kitty_factory, tmp_path):
    from phoenix.core import SnapshotManager
    project = tmp_path / 'project'; project.mkdir()
    marker = tmp_path / 'must-not-exist'
    pane = {'cwd': str(project), 'process': 'unknown', 'scrollback_capture': 'captured'}
    data = {'version': 1, 'active_tab': 0, 'tabs': [{'layout': 'vertical', 'active_pane': 0, 'panes': [pane]}]}
    state = {'windows': [{'class': 'kitty', 'adapters': {'kitty': data}}]}
    manager = SnapshotManager()
    text = f'PHOENIX-HISTORY-MARKER\ntouch {marker}\n'.encode() + b'\x1b]52;c;secret\x07'
    snapshot = manager.save(state, archives=[{'window': 0, 'tab': 0, 'pane': 0, 'data': text}])
    adapter = KittyAdapter({})
    session = adapter.session(data, archive_refs={(0, 0): (snapshot, 0, 0, 0)})
    process, socket = kitty_factory('history-restored', session)
    deadline = time.monotonic() + 8; output = ''
    while time.monotonic() < deadline:
        output = subprocess.run(['kitten', '@', '--to', 'unix:' + socket, 'get-text', '--extent', 'all'], capture_output=True, text=True, check=True, timeout=5).stdout
        if 'PHOENIX-HISTORY-MARKER' in output and 'НОВАЯ ОБОЛОЧКА' in output: break
        time.sleep(.1)
    assert 'PHOENIX-HISTORY-MARKER' in output and 'НОВАЯ ОБОЛОЧКА' in output
    assert 'secret' not in output and not marker.exists()
    assert process.poll() is None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        window = get_tab(socket)['windows'][0]
        if window['cwd'] == str(project) and any(os.path.basename(p.get('cmdline', [''])[0]) in ('bash', 'sh') for p in window.get('foreground_processes', []) if p.get('cmdline')):
            break
        time.sleep(.1)
    else: pytest.fail('New shell did not start in the saved directory after showing history')


def test_real_archives_remain_attached_to_their_own_selected_panes(kitty_factory, tmp_path):
    from phoenix.core import SnapshotManager
    from phoenix.recovery import selected_archive_refs
    dirs = [tmp_path / str(i) for i in range(3)]
    for directory in dirs: directory.mkdir()
    panes = [{'cwd': str(d), 'process': 'unknown', 'scrollback_capture': 'captured'} for d in dirs]
    data = {'version': 1, 'active_tab': 0, 'tabs': [{'layout': 'horizontal', 'active_pane': 1, 'panes': panes}]}
    state = {'windows': [{'class': 'kitty', 'adapters': {'kitty': data}}]}
    manager = SnapshotManager()
    records = [{'window': 0, 'tab': 0, 'pane': i, 'data': f'UNIQUE-HISTORY-{i}\n'.encode()} for i in range(3)]
    sid = manager.save(state, archives=records)
    adapter = KittyAdapter({})
    chosen = {(0, 1), (0, 2)}
    selected = adapter.select(data, chosen)
    refs = selected_archive_refs(sid, 0, data, chosen, {(0, 0, i) for i in range(3)})
    _, socket = kitty_factory('selected-history', adapter.session(selected, archive_refs=refs))
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        windows = get_tab(socket)['windows']
        if len(windows) == 2:
            results = {}
            for window in windows:
                result = subprocess.run(['kitten', '@', '--to', 'unix:' + socket, 'get-text', '--match', f"id:{window['id']}", '--extent', 'all'], capture_output=True, text=True, check=True, timeout=5)
                results[window['cwd']] = result.stdout
            if all(f'UNIQUE-HISTORY-{i}' in results.get(str(dirs[i]), '') for i in (1, 2)): break
        time.sleep(.1)
    else: pytest.fail('Selected panes did not receive their matching archived text')
    assert 'UNIQUE-HISTORY-2' not in results[str(dirs[1])]
    assert 'UNIQUE-HISTORY-1' not in results[str(dirs[2])]
    assert all('UNIQUE-HISTORY-0' not in value for value in results.values())
