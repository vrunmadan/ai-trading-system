@echo off
REM Exit study: 20/30/40% trailing stop vs split 20+40, over the 645-stock universe.
REM Output: streak_backtests\results\exit_study.txt (+ .csv). ~10-15 min.
cd /d "%~dp0"
python -c "import yfinance" 2>nul || python -m pip install yfinance
python streak_backtests\exit_study.py > streak_backtests\results\exit_study_log.txt 2>&1
echo DONE > streak_backtests\results\exit_DONE.txt
echo ALL DONE - you can close this window.
pause
