"""Export completed acceptance into the small policy carried by a release.

Development admission uses the authoritative matrix and its full evidence.
Frozen customer binaries carry only the accepted identity and required runtime
data, rather than the lab's logs, source snapshots or historical test fixtures.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from upgrade.product.launcher import UpgradeAdmissionRefused, admit_static
from upgrade.product.hop import SOURCE, TARGET, DIRECTORY

VERSION_DIRECTORY = 'upgrade/' + TARGET
POLICY_PATH = DIRECTORY / 'release-policy.json'
ACCEPTANCE_IDS = [f'UPG-{number:02d}' for number in range(1, 16)]


def build_inputs_digest(root):
    """Bind distributable source, without private evidence or independent tools."""
    root = Path(root)
    paths = {root/name for name in ['kubecli.py', 'runtime_hook.py', 'build.py',
                                   'kubecli-onefile.spec', 'requirements']}
    for name in ['common', 'controller', 'model', 'service', 'hooks', 'playbooks',
                 'roles', 'conf', 'upgrade/product']:
        paths.update(path for path in (root/name).rglob('*')
                     if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc')
    paths.add(root/VERSION_DIRECTORY/'artifact-pins.json')
    result = hashlib.sha256()
    for path in sorted(paths):
        if path.is_symlink() or not path.is_file():
            raise UpgradeAdmissionRefused('release build input is missing or unsafe: '+str(path.relative_to(root)))
        result.update(str(path.relative_to(root)).encode()+b'\0')
        with path.open('rb') as stream:
            checksum = hashlib.sha256()
            for block in iter(lambda: stream.read(1024*1024), b''): checksum.update(block)
        result.update(checksum.digest())
    return result.hexdigest()


def admit_build(root, matrix_path, policy_path):
    """Verify an exported release in a clean checkout without reopening lab data."""
    policy = admit_packaged(root, policy_path)
    try:
        if policy.get('matrix_sha256') != hashlib.sha256(Path(matrix_path).read_bytes()).hexdigest():
            raise UpgradeAdmissionRefused('release matrix differs from the accepted export')
        if policy.get('build_inputs_sha256') != build_inputs_digest(root):
            raise UpgradeAdmissionRefused('release build inputs differ from the accepted export')
    except OSError as error:
        raise UpgradeAdmissionRefused('accepted release build inputs are missing') from error
    return policy


def runtime_data(root):
    paths = [*sorted((root/'upgrade/product').glob('*.yml')),
             *sorted((root/'upgrade/product/library').glob('*.py')),
             root/'upgrade/product/containerd-config-v3.toml',
             root/VERSION_DIRECTORY/'artifact-pins.json']
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths}


def export(root, matrix_path, output):
    """Refuse a partial release before creating any distributable policy."""
    state = admit_static(matrix_path, required=ACCEPTANCE_IDS)
    if state['pass'] != 15 or state['total'] != 15:
        raise UpgradeAdmissionRefused('release export requires every upgrade acceptance item')
    policy = {'schema_version': 1, 'source': SOURCE, 'target': TARGET,
              'accepted_ids': ACCEPTANCE_IDS, 'runtime_data': runtime_data(root),
              'build_inputs_sha256': build_inputs_digest(root),
              'matrix_sha256': hashlib.sha256(Path(matrix_path).read_bytes()).hexdigest()}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=output.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(policy, stream, sort_keys=True, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
    return policy


def admit_packaged(root, path=POLICY_PATH):
    """Admit a released binary without requiring the development evidence tree."""
    try:
        policy = json.loads(Path(path).read_text())
        if (not isinstance(policy, dict) or policy.get('schema_version') != 1 or
                policy.get('source') != SOURCE or policy.get('target') != TARGET or
                policy.get('accepted_ids') != ACCEPTANCE_IDS or
                not isinstance(policy.get('runtime_data'), dict) or
                policy['runtime_data'] != runtime_data(root)):
            raise UpgradeAdmissionRefused('packaged upgrade policy or runtime data differs from the accepted release')
    except (OSError, ValueError) as error:
        if isinstance(error, UpgradeAdmissionRefused):
            raise
        raise UpgradeAdmissionRefused('accepted upgrade release policy is missing or unreadable') from None
    return policy
