@echo off
REM Multibagger study over the new 645-stock universe (Yahoo Finance data).
REM Output: streak_backtests\results\multibagger_study.txt (+ .csv). ~10-20 min.
cd /d "%~dp0"
python -c "import yfinance" 2>nul || python -m pip install yfinance
python streak_backtests\multibagger_study.py > streak_backtests\results\multibagger_study_log.txt 2>&1
echo DONE > streak_backtests\results\multibagger_DONE.txt
echo ALL DONE - you can close this window.
pause
