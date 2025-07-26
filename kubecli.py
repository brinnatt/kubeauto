import os
from pathlib import Path

from core.cli import KubeautoCLI


def inject_ansible_cfg():
    cfg_path = Path(__file__).parent / "ansible.cfg"
    if not cfg_path.exists():
        raise FileNotFoundError("ansible.cfg not found")
    os.environ["ANSIBLE_CONFIG"] = str(cfg_path)


def main():
    """Entry point for the CLI"""
    inject_ansible_cfg()
    cli = KubeautoCLI()
    cli.run()


if __name__ == "__main__":
    main()
