#!/usr/bin/env python3
"""NetCheckCli against real loopback ICMP/TCP, with owned process groups."""
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time


def probe_arguments(output, targets, duration='8', workers='2'):
    # A real ping batch takes about two seconds. With three ICMP targets and
    # two workers, allow two batches plus scheduling margin before stopping.
    return ['--no-default-nodes','-d',duration,'--max-expand','4','--max-icmp-workers',workers,
            '--report-dir',str(output),'--json-summary',str(output/'summary.json'),
            '--log-file',str(output/'netcheck.log'),*targets]


def verify_result(result, summary, expected_rc, count, ok):
    if (result.returncode != expected_rc or summary['target_count'] != count
            or summary['overall_ok'] is not ok or 'Traceback' in result.stdout):
        raise ValueError('NETCHECK_RESULT_REFUSED rc='+str(result.returncode)+' '+result.stdout[-2000:])


def main():
    root=Path.cwd()
    if root.parent!=Path('/tmp') or not root.name.startswith('tools-netcheck-'):
        raise ValueError('NETCHECK_FIXTURE_SCOPE_REFUSED')
    if not shutil.which('ping'):
        raise ValueError('NETCHECK_REAL_PING_MISSING')
    work=root/'fixture'
    work.mkdir(mode=0o700)
    logs=root/'diagnostics';logs.mkdir(mode=0o700)
    stop=threading.Event()
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(64);listener.settimeout(.2)
    closed=socket.socket();closed.bind(('127.0.0.1',0))
    target='127.0.0.1:'+str(listener.getsockname()[1])
    refused='127.0.0.1:'+str(closed.getsockname()[1])
    processes=[]
    def accept():
        while not stop.is_set():
            try:
                connection,_=listener.accept();connection.close()
            except socket.timeout:
                pass
    thread=threading.Thread(target=accept);thread.start()
    env=dict(os.environ,NETCHECK_LOG_DIR=str(work/'logs'),PYTHONDONTWRITEBYTECODE='1',PYTHONUNBUFFERED='1')
    try:
        for mode in ('source','frozen'):
            tool=([sys.executable,'-B',str(root/'NetCheckCli.py')] if mode=='source' else [str(root/'NetCheckCli')])
            for scenario,targets,count,rc in (('success',['127.0.0.1-2','127.0.0.0/30','localhost',target],4,0),
                                              ('quality',[refused],1,2),
                                              ('unsampled',['127.0.0.1-2','localhost'],3,2)):
                output=work/(mode+'-'+scenario);output.mkdir()
                argv=tool+probe_arguments(output, targets, *(['1','1'] if scenario=='unsampled' else []))
                result=subprocess.run(argv,cwd=work,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=20)
                (logs/(mode+'-'+scenario+'.log')).write_text(result.stdout)
                summary=json.loads((output/'summary.json').read_text())
                verify_result(result,summary,rc,count,scenario=='success')
                if scenario=='unsampled' and not any('无数据' in note for note in summary['notes']):
                    raise ValueError('NETCHECK_UNSAMPLED_TARGET_NOT_REPORTED')
                if not list(output.glob('network_test_report_*.txt')) or not (output/'netcheck.log').is_file():
                    raise ValueError('NETCHECK_REPORT_MISSING')
                (logs/(mode+'-'+scenario+'.json')).write_text(json.dumps(summary))
            for signum in (signal.SIGINT,signal.SIGTERM):
                output=work/(mode+'-'+str(signum)+'.json')
                argv=tool+['--no-default-nodes','--no-log-file','--no-report-file','-d','120',
                           '--json-summary',str(output),target]
                raw=logs/(mode+'-signal-'+str(signum)+'.log')
                with raw.open('w') as stream:
                    process=subprocess.Popen(argv,cwd=work,env=env,text=True,stdout=stream,
                                             stderr=subprocess.STDOUT,start_new_session=True)
                    processes.append(process)
                    deadline=time.monotonic()+10
                    while 'TCP 成功' not in raw.read_text():
                        if process.poll() is not None or time.monotonic()>deadline:
                            raise ValueError('NETCHECK_SIGNAL_SAMPLE_MISSING')
                        stop.wait(.1)
                    process.send_signal(signum)
                    rc=process.wait(timeout=10)
                verify_result(subprocess.CompletedProcess(argv,rc,raw.read_text()),json.loads(output.read_text()),1,1,True)
                try:
                    os.killpg(process.pid,0)
                except ProcessLookupError:
                    pass
                else:
                    raise ValueError('NETCHECK_ORPHAN_PROCESS_GROUP')
            print('NETCHECK_REAL_PROBES_PASS mode='+mode+' icmp=loopback tcp=readback quality_rc=2 signals=INT,TERM',flush=True)
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=10)
        stop.set();thread.join(timeout=3);listener.close();closed.close()
        shutil.rmtree(work)
        if thread.is_alive() or work.exists():raise ValueError('NETCHECK_SCOPED_CLEAN_REFUSED')
        print('NETCHECK_SCOPED_CLEAN_PASS sockets=closed processes=reaped work=removed',flush=True)
    print('NETCHECK_LIVE_PASS modes=2 real_ping=1',flush=True)


if __name__=='__main__':
    main()
