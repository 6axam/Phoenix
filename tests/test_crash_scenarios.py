"""Hard-kill tests use temporary databases and their own test processes."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from phoenix.core import SnapshotManager, update_config
from phoenix.recovery import RecoveryExecutor
from test_phoenix import FakeBackend, FakeRegistry, state, window


def test_killed_mid_snapshot_rolls_back_rotation(tmp_path,monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME',str(tmp_path/'config'))
    update_config({'snapshot_retention':1})
    db=tmp_path/'db'; m=SnapshotManager(db); old=m.save(state(window()),automatic=True)
    code='''import os,sys\nfrom pathlib import Path\nimport phoenix.core as c\nm=c.SnapshotManager(Path(sys.argv[1]))\nc.zlib.compress=lambda *a,**k: os._exit(77)\nm.save({'schema':1,'windows':[{'class':'new'}]},automatic=True,archives=[{'window':0,'tab':0,'pane':0,'data':os.urandom(4096)}])\n'''
    p=subprocess.run([sys.executable,'-c',code,str(db)],env=os.environ.copy())
    assert p.returncode==77
    assert m.integrity()=='ok' and m.get(old)[1]['windows'][0]['class']=='kitty'
    assert [r['id'] for r in m.history()]==[old]


def test_killed_after_launch_does_not_retry(tmp_path):
    db=tmp_path/'db'; marker=tmp_path/'child.pid'; m=SnapshotManager(db); snapshot=state(window()); sid=m.save(snapshot)
    code='''import os,subprocess,sys\nfrom pathlib import Path\nfrom phoenix.core import SnapshotManager\nfrom phoenix.recovery import RecoveryExecutor\nclass B:\n def query(self,name): return {'clients':[], 'monitors':[]}[name]\nclass R:\n def find(self, cls): return Path('/tmp/test.desktop')\n def launch(self, entry):\n  child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True)\n  Path(sys.argv[3]).write_text(str(child.pid))\n  os._exit(78)\nm=SnapshotManager(Path(sys.argv[1])); sid=int(sys.argv[2]); _,s=m.get(sid)\nRecoveryExecutor(B(),R(),m,.01).restore(sid,s)\n'''
    p=subprocess.run([sys.executable,'-c',code,str(db),str(sid),str(marker)])
    try:
        assert p.returncode==78 and marker.exists()
        registry=FakeRegistry(); result=RecoveryExecutor(FakeBackend(),registry,m,.01).restore(sid,snapshot)
        assert result[0]['status']=='uncertain' and registry.launches==0 and m.integrity()=='ok'
    finally:
        if marker.exists():
            try:
                pid=int(marker.read_text())
                arguments=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
                if b'import time; time.sleep(60)' in arguments: os.kill(pid,signal.SIGTERM)
            except (FileNotFoundError, ProcessLookupError): pass
