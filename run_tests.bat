@echo off
echo ================================================
echo  AI Trading System - Connection Tests
echo ================================================
echo.
cd /d "%~dp0"
python setup\test_connections.py
echo.
pause
