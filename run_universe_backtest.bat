@echo off
REM Backtest the breakout engine on the proposed 101-750 universe, by index tier.
REM Same engine, same 20%% trailing exit, costs scaled to liquidity tier.
REM Output: streak_backtests\results\bt_<tier>.txt  (Claude reads these)
cd /d "%~dp0"
python -c "import yfinance" 2>nul || python -m pip install yfinance
echo Running Midcap 150 ...
python streak_backtests\backtest.py --universe streak_backtests\midcap150.csv --trail 20 --cost-bps 50 --by-regime > streak_backtests\results\bt_midcap150.txt 2>&1
echo Running Smallcap 250 ...
python streak_backtests\backtest.py --universe streak_backtests\smallcap250.csv --trail 20 --cost-bps 80 --by-regime > streak_backtests\results\bt_smallcap250.txt 2>&1
echo Running Microcap 250 ...
python streak_backtests\backtest.py --universe streak_backtests\microcap250.csv --trail 20 --cost-bps 120 --by-regime > streak_backtests\results\bt_microcap250.txt 2>&1
echo DONE > streak_backtests\results\bt_DONE.txt
echo ALL DONE - you can close this window.
pause
