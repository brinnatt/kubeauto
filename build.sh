#!/usr/bin/env bash

set -euo pipefail

pip3 install --upgrade pip
pip3 install pyinstaller

pyinstaller --clean kubecli.spec

# 打包完成后：
# dist/kubecli/ 目录里会有可执行文件和附带文件（推荐先在 onedir 下测试）
# 如果确认没问题，再考虑 --onefile：
# pyinstaller --clean --onefile --name kubecli kubecli.py --add-data "playbooks:playbooks" --add-data "roles:roles" --add-data "common:common"