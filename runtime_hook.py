import os
import sys
from pathlib import Path


# PyInstaller will inject sys.frozen attribute
if getattr(sys, 'frozen', False):
    base = Path(sys._MEIPASS)
else:
    base = Path(__file__).resolve().parent


# ansible builtin roles variable
os.environ.setdefault('ANSIBLE_ROLES_PATH', str(base / 'roles'))

# self-defined variable, you have to invoke it by yourself
os.environ.setdefault('ANSIBLE_PLAYBOOKS_PATH', str(base / 'playbooks'))