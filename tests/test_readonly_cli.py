import hashlib
import sqlite3
from pathlib import Path
from phoenix.cli import main
from phoenix.core import HyprlandBackend, SnapshotManager
from test_migration_v4 import legacy


def test_all_readonly_commands_leave_v4_database_and_xdg_untouched(tmp_path,monkeypatch,capsys):
    source=tmp_path/'source'/'db'; source.parent.mkdir(); sid=legacy(source)
    data=tmp_path/'data'; config=tmp_path/'config'; state=tmp_path/'state'
    monkeypatch.setenv('XDG_DATA_HOME',str(data)); monkeypatch.setenv('XDG_CONFIG_HOME',str(config)); monkeypatch.setenv('XDG_STATE_HOME',str(state))
    monkeypatch.setenv('HYPRLAND_INSTANCE_SIGNATURE','readonly-test')
    directory=data/'phoenix'; directory.mkdir(parents=True); dest=directory/'snapshots.sqlite3'
    with sqlite3.connect(source) as src, sqlite3.connect(dest) as dst: src.backup(dst)
    before=hashlib.sha256(dest.read_bytes()).hexdigest(); original_mode=dest.stat().st_mode
    monkeypatch.setattr(HyprlandBackend,'query',lambda self,name: [] if name in ('clients','monitors','workspaces') else None)
    monkeypatch.setattr(HyprlandBackend,'dispatch',lambda *a,**k: (_ for _ in ()).throw(AssertionError('unexpected dispatch')))
    from phoenix.core import DesktopRegistry
    monkeypatch.setattr(DesktopRegistry,'launch',lambda *a,**k: (_ for _ in ()).throw(AssertionError('unexpected launch')))
    for args in (['restore',str(sid),'--dry-run'], ['status'], ['history'], ['inspect',str(sid)], ['doctor'], ['recovery-log',str(sid)], ['archive','list',str(sid)], ['config']):
        assert main(args)==0, args
        capsys.readouterr()
    assert main(['archive','show',str(sid),'0','0','0'])==1
    capsys.readouterr()
    assert hashlib.sha256(dest.read_bytes()).hexdigest()==before and dest.stat().st_mode==original_mode
    with sqlite3.connect(dest.as_uri()+'?mode=ro',uri=True) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==4
        assert db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0]==1
    assert not config.exists() and not state.exists()
    assert not list(directory.glob('*.lock')) and not (directory/'backups').exists()


def test_readonly_commands_do_not_create_a_missing_database(tmp_path,monkeypatch,capsys):
    for k in ('DATA','STATE','CONFIG'): monkeypatch.setenv('XDG_'+k+'_HOME',str(tmp_path/k.lower()))
    assert main(['config'])==0
    monkeypatch.setattr(HyprlandBackend,'query',lambda self,name: [])
    assert main(['history'])==0 and main(['status'])==0 and main(['doctor'])==0
    assert not (tmp_path/'data').exists() and not (tmp_path/'config').exists() and not (tmp_path/'state').exists()


def test_readonly_commands_on_v5_and_archive_show(tmp_path,monkeypatch,capsys):
    source=tmp_path/'source'/'db'; source.parent.mkdir(); manager=SnapshotManager(source)
    sid=manager.save({'schema':1,'windows':[{'class':'kitty'}]},archives=[{'window':0,'tab':0,'pane':0,'data':b'test terminal output'}])
    for key in ('DATA','STATE','CONFIG'): monkeypatch.setenv('XDG_'+key+'_HOME',str(tmp_path/key.lower()))
    target=tmp_path/'data'/'phoenix'/'snapshots.sqlite3'; target.parent.mkdir(parents=True)
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst: src.backup(dst)
    before=hashlib.sha256(target.read_bytes()).hexdigest()
    monkeypatch.setattr(HyprlandBackend,'query',lambda self,name: [])
    for args in (['restore',str(sid),'--dry-run'],['recovery-log',str(sid)],['archive','list',str(sid)],['archive','show',str(sid),'0','0','0']):
        assert main(args)==0, args
        capsys.readouterr()
    assert hashlib.sha256(target.read_bytes()).hexdigest()==before
    assert not list(target.parent.glob('*.lock')) and not (target.parent/'backups').exists()
    assert not (tmp_path/'state').exists() and not (tmp_path/'config').exists()
