@echo off
cd /d "%~dp0"
echo Installing required packages...
pip install pyotp requests kiteconnect python-dotenv -q
echo.
echo Starting Kite authentication setup...
echo.
python setup/setup_kite_auth.py
pause
