#!/usr/bin/env python3
"""Opt-in local benchmark; operates on synthetic text in temporary Kitty instances."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time
from phoenix.adapters.kitty import KittyAdapter
from phoenix.core import SnapshotManager


def worker(count: int) -> dict:
    with tempfile.TemporaryDirectory(prefix='phoenix-bench-') as temp:
        root=Path(temp); private=root/'socket'; private.mkdir(mode=0o700)
        for kind in ('DATA','CONFIG','STATE'): os.environ['XDG_'+kind+'_HOME']=str(root/kind.lower())
        home=root/'home'; home.mkdir(); os.environ['HOME']=str(home); os.environ['HISTFILE']='/dev/null'
        path=str(private/'kitty.sock')
        session='new_tab Benchmark\nlayout vertical\n'+'launch\n'*count
        proc=subprocess.Popen(['kitty','--config','NONE','-o','shell=/bin/bash --noprofile --norc','-o','allow_remote_control=socket-only','--listen-on','unix:'+path,'--session','-'],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        try:
            assert proc.stdin is not None
            proc.stdin.write(session.encode());proc.stdin.close()
            deadline=time.monotonic()+8
            while not Path(path).exists() and time.monotonic()<deadline: time.sleep(.05)
            if not Path(path).exists(): raise RuntimeError('Temporary Kitty did not start')
            adapter=KittyAdapter({'kitty_enabled':True,'kitty_socket':path})
            command=b"head -c 6144 /dev/urandom | base64; printf '\\nPHOENIX_BENCH_DONE\\n'\n"
            subprocess.run(['kitten','@','--to','unix:'+path,'send-text','--match','all','--stdin'],input=command,check=True,timeout=5)
            deadline=time.monotonic()+8
            while True:
                warm, warm_archives=adapter.collect(True,expected_pid=proc.pid)
                if len(warm_archives)==count and all(a['data'].count(b'PHOENIX_BENCH_DONE')>=2 for a in warm_archives): break
                if time.monotonic()>deadline: raise RuntimeError('Temporary terminal history was not ready')
                time.sleep(.1)
            start=time.perf_counter(); metadata,_=adapter.collect(False,expected_pid=proc.pid); metadata_sec=time.perf_counter()-start
            start=time.perf_counter(); result,archives=adapter.collect(True,expected_pid=proc.pid); collection_sec=time.perf_counter()-start
            if not metadata.data or not result.data or len(archives)!=count: raise RuntimeError('Incomplete Kitty collection')
            state={'schema':1,'windows':[{'class':'kitty','adapters':{'kitty':result.data}}]}
            records=[dict(a,window=0) for a in archives]; m=SnapshotManager()
            def checkpoint():
                with m.connect() as db: db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                return m.path.stat().st_size
            before=checkpoint(); start=time.perf_counter(); first=m.save(state,automatic=True,archives=records); write_sec=time.perf_counter()-start; after=checkpoint()
            start=time.perf_counter(); second=m.save(state,automatic=True,archives=records); repeat_sec=time.perf_counter()-start; repeated=checkpoint()
            with m.connect() as db:
                unique_chunks, compressed = db.execute('SELECT COUNT(*),COALESCE(SUM(length(data)),0) FROM archive_chunks').fetchone()
                references = db.execute('SELECT COUNT(*) FROM archive_refs').fetchone()[0]
            sample={'unique_chunks':unique_chunks,'archive_references':references,'compressed_archive_bytes':compressed,'panels':count,'history_bytes':sum(len(a['data']) for a in archives),'metadata_seconds':round(metadata_sec,4),'collection_with_text_seconds':round(collection_sec,4),'write_seconds':round(write_sec,4),'repeat_write_seconds':round(repeat_sec,4),'database_growth_bytes':after-before,'repeat_database_growth_bytes':repeated-after,'same_snapshot':first==second,'python_peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
        finally:
            if proc.poll() is None: proc.terminate()
            try: proc.wait(timeout=3)
            except subprocess.TimeoutExpired: proc.kill();proc.wait()
        sample['max_child_peak_rss_kib']=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        return sample


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--worker',type=int,choices=(1,3,10)); args=parser.parse_args()
    if not os.environ.get('HYPRLAND_INSTANCE_SIGNATURE'): raise SystemExit('A Hyprland session is required')
    if args.worker:
        print(json.dumps(worker(args.worker))); return
    for count in (1,3,10):
        proc=subprocess.run([sys.executable,__file__,'--worker',str(count)],capture_output=True,text=True,timeout=60,check=True)
        print(proc.stdout.strip(),flush=True)

if __name__=='__main__': main()
