# https://pyinstaller.org/en/stable/spec-files.html
# Main app: kubecli. Tools are built separately via tools-onefile.spec.

from pathlib import Path

from common.utils import get_pkg_dir

block_cipher = None

project_root = Path(SPECPATH)

ANSIBLE_RUNNER_DIR = get_pkg_dir('ansible_runner')

# Data files: first path = source (relative to spec dir), second = destination in bundle.
added_files = [
    ('playbooks', 'playbooks'),
    ('roles', 'roles'),
]

a = Analysis(
    ['kubecli.py'],
    pathex=[str(project_root)],
    binaries=[],
    datas=added_files,
    hiddenimports=[],
    hookspath=['hooks'],
    runtime_hooks=['runtime_hook.py'],
    excludes=[],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# One-file mode: no COLLECT; EXE receives all scripts, modules, and binaries.
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