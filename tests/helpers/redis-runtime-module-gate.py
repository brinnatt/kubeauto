#!/usr/bin/env python3
"""Original pinned Redis module inventory/CLI diagnostics in one isolated Pod."""
import argparse
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import signal
import tempfile
import time

SCRIPT = r'''set -eu
set --
count=0
for module in /usr/local/lib/redis/modules/*.so; do
 test -f "$module"
 printf 'REDIS_MODULE_FILE path=%s\n' "$module"
 set -- "$@" --loadmodule "$module"
 count=$((count+1))
done
printf 'REDIS_MODULE_FILE_COUNT value=%s\n' "$count"
redis-server --bind 127.0.0.1 --port 12010 --dir /tmp --save '' --appendonly no "$@" >/tmp/redis-module.log 2>&1 &
owned=$!
trap 'kill "$owned" 2>/dev/null || true; wait "$owned" 2>/dev/null || true' EXIT
attempt=0
while ! redis-cli -p 12010 ping >/dev/null 2>&1; do
 kill -0 "$owned" || { cat /tmp/redis-module.log; exit 1; }
 attempt=$((attempt+1)); test "$attempt" -lt 100; sleep 0.2
done
printf 'REDIS_MODULE_JSON '
redis-cli -e --json -p 12010 MODULE LIST
printf 'REDIS_MODULE_RESP2_JSON '
redis-cli -2 -e --json -p 12010 MODULE LIST
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', choices=('runtime-modules',))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    args = parser.parse_args()
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    spec = importlib.util.spec_from_file_location('redis_modules_focused', args.base/'tests/helpers/redis-focused-gate.py')
    focused = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(focused)
    with tempfile.TemporaryDirectory(prefix='redis-module-probe-') as tmp, ExitStack() as stack:
        source = focused.Gate(args, Path(tmp), stack)
        source.baseline()
        name = 'redis-module-probe-' + source.run_id[:16]
        pod = {'apiVersion':'v1', 'kind':'Pod', 'metadata':{'name':name, 'namespace':source.ns,
               'labels':{'kubeauto.io/component':'redis-test', 'kubeauto.io/managed-by':'kubeauto',
                         'kubeauto.io/test-run':source.run_id}},
               'spec':{'restartPolicy':'Never', 'containers':[{'name':'probe',
                       'image':'registry.talkschool.cn:5000/brinnatt/redis:8.10.2',
                       'command':['sh','-ec',SCRIPT],
                       'resources':{'requests':{'cpu':'10m','memory':'64Mi'},
                                    'limits':{'cpu':'1','memory':'512Mi'}}}]}}
        source.k('create', '-f', '-', input_text=json.dumps(pod))
        try:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                obj = source.get('pod', name)
                phase = obj.get('status', {}).get('phase')
                if phase in ('Succeeded', 'Failed'):
                    print(source.k('-n', source.ns, 'logs', name), flush=True)
                    if phase == 'Failed':
                        raise ValueError('original Redis module probe failed; preserve actual startup diagnostics')
                    break
                time.sleep(1)
            else:
                raise ValueError('isolated original-image module probe timed out')
        finally:
            obj = source.get('pod', name)
            if obj['metadata']['labels'].get('kubeauto.io/test-run') != source.run_id:
                raise ValueError('module probe cleanup identity changed')
            source.k('-n', source.ns, 'delete', 'pod', name, '--wait=true', '--timeout=90s', timeout=110)
        source.baseline()
    print('REDIS_FOCUSED_SCOPED_CLEAN_PASS module=runtime-modules', flush=True)
    print('REDIS_FOCUSED_MODULE_PASS module=runtime-modules acceptance=diagnostic-only', flush=True)


if __name__ == '__main__':
    main()
