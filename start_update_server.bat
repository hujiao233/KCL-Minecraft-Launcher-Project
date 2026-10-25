@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PORT=8756
if exist "runtime\python.exe" (set "PY=runtime\python.exe") else (set "PY=python")
echo ============================================
echo   Cuibing Launcher - Update Server
echo   port: %PORT%
echo ============================================
"%PY%" update_server.py --host 0.0.0.0 --port %PORT% --open
echo.
echo Server stopped.
pause
