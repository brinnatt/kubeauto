import sys
from pathlib import Path


block_cipher = None

project_root = Path(SPECPATH)

a = Analysis(
    ['kubecli.py'],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        ('playbooks', 'playbooks'),
        ('roles', 'roles'),
        ('common', 'common'),
        ('pics', 'pics'),
        ('/usr/local/lib/python3.12/site-packages/ansible_runner', 'ansible_runner')
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