import sys
from common.utils import get_pkg_dir
from pathlib import Path


block_cipher = None

project_root = Path(SPECPATH)

ANSIBLE_RUNNER_DIR = get_pkg_dir('ansible_runner')

a = Analysis(
    ['kubecli.py'],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        ('playbooks', 'playbooks'),
        ('roles', 'roles'),
        (ANSIBLE_RUNNER_DIR, 'ansible_runner')
    ],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=['runtime_hook.py'],
    excludes=[],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    exclude_binaries=False,
    name='kubecli',
    debug=False,
    strip=False,
    upx=True,
    console=True,
    onefile=True,
)