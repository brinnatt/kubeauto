# https://pyinstaller.org/en/stable/spec-files.html
# Each .py under tools/ is built as a separate onefile executable. Independent of kubecli (see kubecli-onefile.spec).

from pathlib import Path

block_cipher = None

project_root = Path(SPECPATH)
tools_dir = project_root / "tools"

# Collect all .py scripts under tools/ (including subdirs, e.g. tools/xtrabackup/mysqlbackup.py).
tool_scripts = sorted(tools_dir.rglob("*.py"))

# Per-script Analysis/PYZ/EXE with unique names so PyInstaller builds every target (spec is executable Python; doc recommends unique names per program).
for script_path in tool_scripts:
    script_str = str(script_path.resolve())
    name = script_path.stem
    a = Analysis(
        [script_str],
        pathex=[str(project_root)],
        binaries=[],
        datas=[],
        hiddenimports=[],
        hookspath=None,
        runtime_hooks=None,
        excludes=None,
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
        name=name,
        debug=False,
        strip=False,
        upx=True,
        console=True,
        onefile=True,
    )
    globals()["exe_" + name] = exe
