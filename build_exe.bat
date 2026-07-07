@echo off
pip install -r requirements.txt pyinstaller
pyinstaller dualrip.spec --noconfirm
echo.
echo Build done: dist\DualRip.exe
pause
