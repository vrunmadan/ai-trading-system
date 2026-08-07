@echo off
echo ================================================
echo  Kite Connect - Daily Token Refresh
echo ================================================
echo.
echo This will open your browser to log in to Kite.
echo After login, Zerodha redirects to localhost:8080
echo Copy the request_token from that URL and paste it here.
echo.
cd /d "%~dp0"
python setup\refresh_kite_token.py
echo.
pause
