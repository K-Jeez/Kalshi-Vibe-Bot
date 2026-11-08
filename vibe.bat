@echo off
setlocal
cd /d "%~dp0"
set "ROOT=%~dp0"
set "PYEXE=%ROOT%venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=%ROOT%.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
"%PYEXE%" "%ROOT%backend\tools\dev_control.py" %*
exit /b %ERRORLEVEL%
