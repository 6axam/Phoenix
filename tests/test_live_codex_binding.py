"""Opt-in integration: isolated Kitty and a synthetic Codex executable."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time
import pytest
from test_kitty_integration import kitty_factory
from phoenix.adapters.kitty import KittyAdapter
from phoenix.core import SnapshotManager

pytestmark = pytest.mark.skipif(os.environ.get('PHOENIX_KITTY_INTEGRATION') != '1' or not os.environ.get('HYPRLAND_INSTANCE_SIGNATURE') or not shutil.which('kitty') or not shutil.which('cc'), reason='requires isolated live Kitty and C compiler')
IDENT = 'a04f5046-8ea3-4c04-bda8-49fa089858c5'


def test_explicit_binding_only_exists_while_test_process_is_foreground(tmp_path, monkeypatch, kitty_factory):
    directory = tmp_path / 'bin'; directory.mkdir()
    project = tmp_path / 'project'; project.mkdir()
    server = tmp_path / 'fake_server.py'
    server.write_text('''import json, os, sys
for line in sys.stdin:
 request=json.loads(line)
 if request['method']=='initialize': print(json.dumps({'id':1,'result':{}}),flush=True)
 if request['method']=='thread/read':
  print(json.dumps({'id':2,'result':{'thread':{'id':request['params']['threadId'],'cwd':os.environ['PHOENIX_TEST_PROJECT'],'ephemeral':False}}}),flush=True)
''')
    source = tmp_path / 'fake.c'
    source.write_text('''#include <unistd.h>\n#include <string.h>\nint main(int n, char **v) { if(n > 1 && !strcmp(v[1], "app-server")) { execl("/usr/bin/python3", "python3", v[2], (char*)0); return 1; } if(n > 1 && !strcmp(v[1], "resume")) { sleep(5); return 0; } return 1; }\n'''.replace('v[2]', json.dumps(str(server))))
    subprocess.run(['cc', str(source), '-o', str(directory / 'codex')], check=True, timeout=10)
    monkeypatch.setenv('PATH', str(directory) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('PHOENIX_TEST_PROJECT', str(project))
    monkeypatch.setenv('PYTHONPATH', str(Path(__file__).resolve().parents[1]))
    _, socket = kitty_factory('codex-binding', f'new_tab Test\nlaunch --cwd={project}\n')
    command = f'/usr/bin/python3 -m phoenix codex-resume {IDENT} --cwd {shlex.quote(str(project))}\n'
    subprocess.run(['kitten', '@', '--to', 'unix:' + socket, 'send-text', '--stdin'], input=command.encode(), check=True, timeout=5)
    adapter = KittyAdapter({'kitty_enabled': True, 'kitty_socket': socket, 'kitty_codex_bindings': True})
    end = time.monotonic() + 5; data = None
    while time.monotonic() < end:
        result, _ = adapter.collect()
        if result.data and result.data['tabs'][0]['panes'][0].get('codex_session'):
            data = result.data; break
        time.sleep(.05)
    assert data, 'Explicit binding was not detected in the isolated pane'
    assert data['tabs'][0]['panes'][0]['codex_session'] == {'session_id': IDENT, 'source': 'explicit_resume'}
    assert 'wrapper_start' not in json.dumps(data) and 'child_start' not in json.dumps(data)
    manager = SnapshotManager()
    snapshot = manager.save({'windows': [{'class': 'kitty', 'adapters': {'kitty': data}}]})
    end = time.monotonic() + 8
    while time.monotonic() < end:
        result, _ = adapter.collect()
        if result.data and not result.data['tabs'][0]['panes'][0].get('codex_session'): break
        time.sleep(.1)
    else: pytest.fail('Binding was retained after the test process exited')
    restore = f'/usr/bin/python3 -m phoenix codex-restore {snapshot} 0 0 0\n'
    subprocess.run(['kitten', '@', '--to', 'unix:' + socket, 'send-text', '--stdin'], input=restore.encode(), check=True, timeout=5)
    end = time.monotonic() + 5
    while time.monotonic() < end:
        result, _ = adapter.collect()
        if result.data and result.data['tabs'][0]['panes'][0].get('codex_session'):
            assert result.data['tabs'][0]['panes'][0]['codex_session']['session_id'] == IDENT
            break
        time.sleep(.05)
    else: pytest.fail('Explicit restore did not resume the same test session in the selected pane')
