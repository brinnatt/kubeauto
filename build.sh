#!/usr/bin/env bash

set -euo pipefail

pip3 install --upgrade pip
pip3 install pyinstaller

pyinstaller --clean kubecli.spec
