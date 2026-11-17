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

VERSION_DIRECTORY = 'upgrade/v1.35.10'
POLICY_PATH = Path(__file__).parent.parent / 'v1.35.10/release-policy.json'
ACCEPTANCE_IDS = [f'UPG-{number:02d}' for number in range(1, 16)]


def runtime_data(root):
    paths = [*sorted((root/'upgrade/product').glob('*.yml')),
             root/'upgrade/product/containerd-config-v3.toml',
             root/VERSION_DIRECTORY/'artifact-pins.json']
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths}


def export(root, matrix_path, output):
    """Refuse a partial release before creating any distributable policy."""
    state = admit_static(matrix_path, required=ACCEPTANCE_IDS)
    if state['pass'] != 15 or state['total'] != 15:
        raise UpgradeAdmissionRefused('release export requires every upgrade acceptance item')
    policy = {'schema_version': 1, 'source': 'v1.34.13', 'target': 'v1.35.10',
              'accepted_ids': ACCEPTANCE_IDS, 'runtime_data': runtime_data(root),
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
                policy.get('source') != 'v1.34.13' or policy.get('target') != 'v1.35.10' or
                policy.get('accepted_ids') != ACCEPTANCE_IDS or
                not isinstance(policy.get('runtime_data'), dict) or
                policy['runtime_data'] != runtime_data(root)):
            raise UpgradeAdmissionRefused('packaged upgrade policy or runtime data differs from the accepted release')
    except (OSError, ValueError) as error:
        if isinstance(error, UpgradeAdmissionRefused):
            raise
        raise UpgradeAdmissionRefused('accepted upgrade release policy is missing or unreadable') from None
    return policy
