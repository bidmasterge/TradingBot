@echo off
cd /d "%~dp0"
git add -A
git commit -m "Update Trading Bot"
git push -u origin main
pause
