#!/usr/bin/env python3
import sys
from pathlib import Path
from common.utils import run_command
from common.exceptions import CommandExecutionError
from common.logger import setup_logger

logger = setup_logger(__name__)

def main():
    project_root = Path(__file__).resolve().parent
    spec_file = project_root / "kubecli-onefile.spec"

    try:
        logger.info("Upgrading pip...", extra={"to_stdout": True})
        run_command([sys.executable, "-m", "pip", "install", "--upgrade", "pip"], capture_output=False)

        # 安装 pyinstaller
        logger.info("Installing PyInstaller...", extra={"to_stdout": True})
        run_command([sys.executable, "-m", "pip", "install",
            "pyinstaller==6.16.0",
            "ansible==9.2.0",
            "ansible-core==2.16.3",
            "ansible-runner==2.4.1",
            "distro==1.9.0",
            "docker==7.1.0",
            "paramiko==4.0.0",
            "psutil==7.0.0"
        ], capture_output=False)

        # 打包
        logger.info(f"Building with spec: {spec_file}", extra={"to_stdout": True})
        run_command([sys.executable, "-m", "PyInstaller", "--clean", str(spec_file)], capture_output=False)

        logger.info("Build finished successfully ✅", extra={"to_stdout": True})

    except CommandExecutionError as e:
        logger.error(f"Build failed {e}.", extra={"to_stdout": True})
        sys.exit(1)

if __name__ == "__main__":
    main()
