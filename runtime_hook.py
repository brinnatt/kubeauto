import os
import sys
from pathlib import Path


# PyInstaller will inject sys.frozen attribute
if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    base = Path(sys._MEIPASS)
else:
    base = Path(__file__).resolve().parent


# ansible builtin roles variable
os.environ.setdefault('ANSIBLE_ROLES_PATH', str(base / 'roles'))

# Use internal ansible-playbook via Python API
os.environ.setdefault("RUNNER_BINARY", f"{sys.executable} -m ansible.cli.playbook")