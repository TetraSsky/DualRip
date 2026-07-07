#!/usr/bin/env bash
set -e
pip install -r requirements.txt pyinstaller
pyinstaller dualrip_linux.spec --noconfirm
echo
echo "Build done: dist/DualRip"
