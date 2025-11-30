# https://pyinstaller.org/en/stable/

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
        ('roles', 'roles')
    ],
    hiddenimports=[],
    # 指定包含 PyInstaller hook 文件的目录，在分析阶段使用；在构建时分析依赖关系，通常包含 collect_submodules(), collect_data_files() 等函数。
    hookspath=['hooks'],
    # 指定在可执行文件启动时运行的 Python 脚本，设置环境变量、修复打包后的运行时问题、动态修改 Python 路径、预加载模块等。
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