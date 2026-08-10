@echo off
cd /d "%~dp0"
git push origin main
if %ERRORLEVEL% EQU 0 (echo PUSHED) else (echo FAILED - exit code %ERRORLEVEL%)
pause
