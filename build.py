#!/usr/bin/env python3
import sys
from pathlib import Path
from common.utils import run_command
from common.exceptions import CommandExecutionError

def main():
    project_root = Path(__file__).resolve().parent
    spec_file = project_root / "kubecli-onefile.spec"

    try:
        print("[INFO] Upgrading pip...")
        run_command([sys.executable, "-m", "pip", "install", "--upgrade", "pip"], check=True)

        # 安装 pyinstaller
        print("[INFO] Installing PyInstaller...")
        run_command([sys.executable, "-m", "pip", "install", "pyinstaller"], check=True)

        # 打包
        print(f"[INFO] Building with spec: {spec_file}")
        run_command([sys.executable, "-m", "PyInstaller", "--clean", str(spec_file)], check=True)

        print("[INFO] Build finished successfully ✅")

    except CommandExecutionError as e:
        print(f"[ERROR] Build failed:\n{e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
