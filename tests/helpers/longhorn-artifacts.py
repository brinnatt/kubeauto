#!/usr/bin/env python3
"""Read-only pinned publication and current China registry verification."""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
REPO = ROOT.parent / 'kubeauto-ext-images-dockerfile'
ACCEPT = 'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json'


def git(*args):
    return subprocess.check_output(['git', '-C', str(REPO), *args])


def fingerprint(row, head):
    h = hashlib.sha256(json.dumps(row, sort_keys=True).encode())
    policy = git('show', head + ':build-policy.json')
    if policy != (REPO / 'build-policy.json').read_bytes():
        raise ValueError('build policy differs from published inputs')
    h.update(policy)
    context = row['ctx'].removeprefix('./').rstrip('/')
    entries = []
    for entry in git('ls-tree', '-rz', head, '--', context).split(b'\0'):
        if not entry: continue
        meta, path = entry.split(b'\t', 1)
        mode, kind, oid = meta.decode().split(); path = path.decode()
        if kind != 'blob' or mode not in ('100644', '100755'):
            raise ValueError('unsupported artifact input type')
        entries.append((path, mode, oid))
    if not entries: raise ValueError('missing artifact build context')
    for path, mode, oid in sorted(entries):
        data = git('cat-file', 'blob', oid)
        if data != (REPO / path).read_bytes(): raise ValueError('artifact source changed: ' + path)
        h.update(path.removeprefix(context + '/').encode() + b'\0')
        h.update(str(int(mode, 8) & 0o777).encode() + b'\0'); h.update(data)
    return h.hexdigest()


def read_manifest(ref):
    if not ref.startswith('hub.talkedu.cn/kubeauto/'):
        raise ValueError('unexpected artifact registry')
    repository, tag = ref.split('/', 1)[1].rsplit(':', 1)
    url = 'https://hub.talkedu.cn/v2/' + repository + '/manifests/' + tag
    headers = {'Accept': ACCEPT}
    try:
        response = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30)
    except urllib.error.HTTPError as error:
        if error.code != 401: raise
        fields = dict(re.findall(r'(\w+)="([^"]*)"', error.headers.get('WWW-Authenticate', '')))
        realm = fields.get('realm', '')
        parsed = urllib.parse.urlsplit(realm)
        if parsed.scheme != 'https' or parsed.hostname != 'hub.talkedu.cn':
            raise ValueError('unexpected registry authentication realm')
        query = urllib.parse.urlencode({'service': fields.get('service', ''),
                                       'scope': fields.get('scope', 'repository:' + repository + ':pull')})
        with urllib.request.urlopen(realm + '?' + query, timeout=30) as stream:
            auth = json.load(stream)
        headers['Authorization'] = 'Bearer ' + auth.get('token', auth.get('access_token', ''))
        response = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30)
    with response as stream:
        raw = stream.read(); digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        if stream.headers.get('Docker-Content-Digest', digest) != digest:
            raise ValueError('registry content digest mismatch')
        index = json.loads(raw)
    platforms = {m['platform']['os'] + '/' + m['platform']['architecture']: m['digest']
                 for m in index.get('manifests', []) if m.get('platform', {}).get('architecture') in ('amd64', 'arm64')}
    return digest, platforms


def main():
    sys.path.insert(0, str(ROOT))
    from common.constants import KubeConstant
    evidence = json.loads((ROOT / 'tests/evidence/longhorn-images-20261021.json').read_text())
    records = evidence['images']
    expected = set(KubeConstant().component_images['longhorn'])
    if evidence['status'] != 'pass' or len(records) != len(expected) or {r['key'] for r in records} != expected:
        raise ValueError('artifact evidence does not cover exact runtime/upgrade image inventory')
    for run in evidence['runs']:
        if run['workflow_conclusion'] != 'success' or run['durable_rc'] or run['failure_markers']:
            raise ValueError('published run is not successful')
    for source in ('mongodb-support-images-20261015.json', 'redis-documentation-client-images-20261014.json'):
        parent = json.loads((ROOT / 'tests/evidence' / source).read_text())
        if parent['workflow_conclusion'] != 'success' or parent['durable_rc'] or parent['failure_markers']:
            raise ValueError('support artifact publication failed')
        records += parent['images']
    if len(records) != 30 or len({r['key'] for r in records}) != 30:
        raise ValueError('incomplete or duplicate support artifact inventory')
    rows = {r['image'] + ':' + r['tag']: r for r in json.loads((REPO / 'catalog.json').read_text())}
    for record in records:
        row = rows[record['key']]
        if record['fingerprint'] != fingerprint(row, record['head']):
            raise ValueError('published artifact fingerprint differs')
        if set(record['platforms']) != {'linux/amd64', 'linux/arm64'}:
            raise ValueError('published artifact lacks both architectures')
    # Formal Docker Hub readback was performed by the bound Actions run;
    # this gate independently reads the China delivery tag without rebuilding.
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(read_manifest, r['talkedu_ref']): r for r in records}
        for future in concurrent.futures.as_completed(futures):
            record = futures[future]; digest, platforms = future.result()
            if digest != record['digest'] or platforms != record['platforms']:
                raise ValueError('current TalkEdu tag differs: ' + record['key'])
            print('LONGHORN_ARTIFACT_VERIFIED image=' + record['key'] + ' digest=' + digest, flush=True)
    print('LONGHORN_ARTIFACTS_SCOPED_CLEAN_PASS')
    print('LONGHORN_ARTIFACTS_PASS images=' + str(len(records)))


if __name__ == '__main__':
    try: main()
    except Exception as error:
        # Do not include HTTP headers, URLs with authentication or response bodies.
        print('LONGHORN_ARTIFACTS_FAIL class=supply-chain reason=' + type(error).__name__, file=sys.stderr)
        sys.exit(1)
