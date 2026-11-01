import os
import sqlite3
import subprocess
import sys
from pathlib import Path
import pytest
from phoenix.core import SnapshotManager, lock


def legacy(path):
    manager=SnapshotManager(path); sid=manager.save({'schema':1,'windows':[{'class':'Legacy'}]})
    with manager.connect() as db:
        for table in ('archive_refs','archive_manifests','archive_chunks','recovery_steps'):
            db.execute('DROP TABLE '+table)
        db.execute('PRAGMA user_version=4')
    return sid


def version(path):
    with sqlite3.connect(path) as db: return db.execute('PRAGMA user_version').fetchone()[0]


def test_v4_migration_preserves_verified_backup(tmp_path):
    path=tmp_path/'db'; sid=legacy(path); m=SnapshotManager(path)
    files=list((tmp_path/'backups'/'migrations').glob('schema4-*.sqlite3'))
    assert len(files)==1 and version(files[0])==4 and version(path)==5
    assert files[0].stat().st_mode & 0o077 == 0
    assert SnapshotManager(files[0],legacy_readonly=True).get(sid)[1]['windows'][0]['class']=='Legacy'
    for _ in range(4): m.backup()
    assert files[0].exists()


def test_legacy_diagnostics_and_running_daemon_guard(tmp_path,monkeypatch):
    monkeypatch.setenv('XDG_DATA_HOME',str(tmp_path/'data')); monkeypatch.setenv('XDG_STATE_HOME',str(tmp_path/'state'))
    path=tmp_path/'data'/'phoenix'/'snapshots.sqlite3'; path.parent.mkdir(parents=True)
    sid=legacy(path)
    with lock(tmp_path/'state'/'phoenix'/'daemon.lock'):
        assert SnapshotManager(legacy_readonly=True).get(sid)[1]['windows']
        assert version(path)==4
        with pytest.raises(RuntimeError,match='daemon is running'): SnapshotManager()
    assert version(path)==4
    assert SnapshotManager().get(sid)[1]['windows'] and version(path)==5


def test_interrupted_migration_rolls_back_and_keeps_backup(tmp_path):
    path=tmp_path/'db'; sid=legacy(path)
    script='''import os,sys\nfrom pathlib import Path\nfrom phoenix.core import SnapshotManager\ndef crash(self, db):\n db.execute("BEGIN IMMEDIATE")\n db.execute("CREATE TABLE interrupted (x INTEGER)")\n os._exit(37)\nSnapshotManager._upgrade_v4=crash\nSnapshotManager(Path(sys.argv[1]))\n'''
    p=subprocess.run([sys.executable,'-c',script,str(path)],cwd=Path(__file__).resolve().parents[1])
    assert p.returncode==37 and version(path)==4
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='interrupted'").fetchone()[0]==0
    backups=list((tmp_path/'backups'/'migrations').glob('schema4-*.sqlite3'))
    assert len(backups)==1 and version(backups[0])==4
    assert SnapshotManager(path,legacy_readonly=True).get(sid)[1]['windows']


def test_failed_migration_preserves_source_and_reports_error(tmp_path,monkeypatch):
    path=tmp_path/'db'; sid=legacy(path)
    def fail(self, db):
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE unfinished (x INTEGER)')
        raise sqlite3.OperationalError('simulated failure')
    monkeypatch.setattr(SnapshotManager,'_upgrade_v4',fail)
    with pytest.raises(RuntimeError,match='migration failed'): SnapshotManager(path)
    assert version(path)==4
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='unfinished'").fetchone()[0]==0
    assert len(list((tmp_path/'backups'/'migrations').glob('schema4-*.sqlite3'))) == 1
    assert SnapshotManager(path,legacy_readonly=True).get(sid)[1]['windows']


def test_migration_blocks_daemon_using_other_state_dir(tmp_path,monkeypatch):
    import phoenix.core as core
    monkeypatch.setenv('XDG_DATA_HOME',str(tmp_path/'data')); monkeypatch.setenv('XDG_STATE_HOME',str(tmp_path/'state'))
    path=tmp_path/'data'/'phoenix'/'snapshots.sqlite3'; path.parent.mkdir(parents=True); legacy(path)
    monkeypatch.setattr(core,'running_daemons_for',lambda path:[12345])
    with pytest.raises(RuntimeError,match='another Phoenix daemon'): SnapshotManager()
    assert version(path)==4
