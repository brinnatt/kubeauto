#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path
from common.utils import run_command
from common.exceptions import CommandExecutionError
from common.logger import setup_logger

logger = setup_logger(__name__)

def main():
    parser = argparse.ArgumentParser(description="Build kubeauto onefile executables")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--kubecli-only", action="store_true")
    target.add_argument("--tools-only", action="store_true")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent
    requirements_file = project_root / "requirements"
    spec_file = project_root / "kubecli-onefile.spec"

    try:
        logger.info("Upgrading pip...", extra={"to_stdout": True})
        run_command([sys.executable, "-m", "pip", "install", "--upgrade", "pip"], capture_output=False)

        logger.info(
            "Installing PyInstaller and pinned runtime dependencies...",
            extra={"to_stdout": True},
        )
        run_command(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "pyinstaller==6.16.0",
                "-r",
                str(requirements_file),
            ],
            capture_output=False,
        )

        if not args.tools_only:
            logger.info(f"Building with spec: {spec_file}", extra={"to_stdout": True})
            run_command(
                [sys.executable, "-m", "PyInstaller", "--clean", str(spec_file)],
                capture_output=False,
            )

        tools_spec = project_root / "tools-onefile.spec"
        if not args.kubecli_only and tools_spec.exists():
            logger.info(f"Building tools with spec: {tools_spec}", extra={"to_stdout": True})
            run_command([sys.executable, "-m", "PyInstaller", "--clean", str(tools_spec)], capture_output=False)

        logger.info("Build finished successfully ✅", extra={"to_stdout": True})

    except CommandExecutionError as e:
        logger.error(f"Build failed {e}.", extra={"to_stdout": True})
        sys.exit(1)

if __name__ == "__main__":
    main()
