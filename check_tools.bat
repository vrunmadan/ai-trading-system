@echo off
echo === Checking tools ===
where npm 2>nul && npm --version || echo npm: NOT FOUND
where node 2>nul && node --version || echo node: NOT FOUND
where git 2>nul && git --version || echo git: NOT FOUND
where python 2>nul && python --version || echo python: NOT FOUND
echo.
pause
