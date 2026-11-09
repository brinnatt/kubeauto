#!/usr/bin/env python3
"""Stream one fixed MigrationCli job and retain its durable outcome locally."""
import hashlib
import pathlib
import subprocess
import sys
import time


def main():
    host, remote, log_name = sys.argv[1:]
    if host not in ('ubuntu@192.168.47.138', 'root@192.168.122.2') or remote != '/tmp/kubeauto-migration-release':
        raise SystemExit('MIGRATION_SUPERVISOR_SCOPE_REFUSED')
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', host]
    sudo = "sudo -n " if host.startswith("ubuntu@") else ""
    started = last_progress = time.monotonic()
    offset = 1
    log = pathlib.Path(log_name)
    with log.open('wb') as output:
        while time.monotonic() - started < 3600:
            result = subprocess.run(ssh + ["tail -c +{} '{}/console.log'".format(offset, remote)], capture_output=True, timeout=25)
            if result.returncode:
                raise SystemExit('MIGRATION_MONITOR_LOST; preserve remote job and inspect durable state')
            if result.stdout:
                output.write(result.stdout); output.flush()
                sys.stdout.buffer.write(result.stdout); sys.stdout.buffer.flush()
                offset += len(result.stdout)
                last_progress = time.monotonic()
            status = subprocess.run(ssh + ["test -s '{0}/state.finalized' && cat '{0}/state.exit' '{0}/state.finalized'".format(remote)], capture_output=True, timeout=25)
            if status.returncode == 0:
                outcome = status.stdout.decode().split()
                archive = log.with_suffix('.evidence.tar.gz')
                subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', host + ':' + remote + '/release-evidence.tar.gz', str(archive)], check=True, timeout=45)
                expected = subprocess.run(ssh + ["sha256sum '{}/release-evidence.tar.gz'".format(remote)], capture_output=True, check=True, timeout=25).stdout.decode().split()[0]
                if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
                    raise SystemExit('MIGRATION_EVIDENCE_CHECKSUM_FAILED')
                # Read the terminal marker after the durable fence before cleanup.
                remaining = subprocess.run(ssh + ["tail -c +{} '{}/console.log'".format(offset, remote)], capture_output=True, check=True, timeout=25).stdout
                output.write(remaining); output.flush()
                sys.stdout.buffer.write(remaining); sys.stdout.buffer.flush()
                log.with_suffix('.remote-status').write_text(' '.join(outcome) + '\n')
                subprocess.run(ssh + ["{}rm -rf '{}'".format(sudo, remote)], check=True, timeout=25)
                subprocess.run(ssh + ["test ! -e '{}'".format(remote)], check=True, timeout=25)
                print('MIGRATION_EVIDENCE_COLLECTED sha256=' + expected, flush=True)
                return 0 if outcome == ['0', '0'] else 1
            if time.monotonic() - last_progress > 60:
                diagnostics = subprocess.run(ssh + [sudo + "ps -eo pid,ppid,etime,comm | head -25"], capture_output=True, timeout=25)
                sys.stdout.buffer.write(diagnostics.stdout); sys.stdout.buffer.flush()
                print('MIGRATION_HEARTBEAT elapsed_seconds={} last_log_bytes={} next=durable-fence'.format(int(time.monotonic()-started), offset-1), flush=True)
                last_progress = time.monotonic()
            time.sleep(5)
    raise SystemExit('MIGRATION_TIMEOUT; preserve remote job and inspect first stalled command')


if __name__ == '__main__':
    raise SystemExit(main())
