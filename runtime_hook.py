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


# 运行时，使用 stevedore 调试 taskflow.engines
# try:
#     from stevedore import extension
#     mgr = extension.ExtensionManager('taskflow.engines', invoke_on_load=False)
#     engines = list(mgr.names())
#     print(f"DEBUG: Found taskflow engines: {engines}", file=sys.stderr)
# except Exception as e:
#     print(f"DEBUG: Engine discovery: {e}", file=sys.stderr)