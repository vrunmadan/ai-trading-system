@echo off
echo ================================================
echo  AI Trading System - Installing Python packages
echo ================================================
echo.

:: Check Python is available
python --version
if errorlevel 1 (
    echo ERROR: Python not found. Please install Python from python.org
    pause
    exit /b 1
)

echo.
echo Installing packages from requirements.txt...
echo.

python -m pip install --upgrade pip
python -m pip install -r "%~dp0requirements.txt"

echo.
if errorlevel 1 (
    echo ================================================
    echo  SOME PACKAGES FAILED - check errors above
    echo ================================================
) else (
    echo ================================================
    echo  All packages installed successfully!
    echo  Next step: python setup\refresh_kite_token.py
    echo ================================================
)

echo.
pause
