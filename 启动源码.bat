@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title 脆冰启动器 - 源码运行

echo ============================================
echo   脆冰启动器 · 从源码运行
echo ============================================
echo.

rem ---------- 1. 找 Python ----------
set "PYCMD="
py -3 -c "import sys" >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD python -c "import sys" >nul 2>nul && set "PYCMD=python"
if not defined PYCMD python3 -c "import sys" >nul 2>nul && set "PYCMD=python3"

if not defined PYCMD if exist "runtime\python.exe" set "PYCMD=runtime\python.exe"

if defined PYCMD goto HAVE_PY

echo [1/3] 没有检测到 Python，正在准备便携环境（约 12 MB，只需一次）...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0prepare_python.ps1"
if exist "runtime\python.exe" (
  set "PYCMD=runtime\python.exe"
  goto HAVE_PY
)

echo.
echo 便携环境也失败了。两条路：
echo   1）到 https://www.python.org/downloads/ 装 Python 3.11+（记得勾 Add to PATH）
echo   2）不想装 Python 就直接用打包好的 EXE（发行版里下载）
echo.
pause
exit /b 1

:HAVE_PY
echo [1/3] 使用 %PYCMD%
%PYCMD% -c "import sys;print('      Python', sys.version.split()[0])"

rem ---------- 2. 装依赖 ----------
echo [2/3] 检查依赖（flask / requests / pywebview）...
%PYCMD% -c "import flask, requests, webview" >nul 2>nul
if errorlevel 1 (
  %PYCMD% -m pip install -r requirements.txt
) else (
  echo      依赖已就绪
)

rem ---------- 3. 启动 ----------
echo [3/3] 启动脆冰启动器...
echo.
%PYCMD% main.py %*
if errorlevel 1 (
  echo.
  echo 启动失败，上面是错误信息。常见问题：
  echo   · pywebview 装不上  -  %PYCMD% -m pip install --upgrade pywebview
  echo   · 端口被占用        -  重开一次即可，会自动换端口
  echo.
  pause
)
