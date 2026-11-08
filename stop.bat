@echo off
setlocal
cd /d "%~dp0"
echo.
echo Stopping Kalshi Vibe Bot processes started by vibe.bat or start.bat.
echo This does not cancel resting Kalshi orders.
echo This does not press Play, Pause, or Stop.
echo.
call "%~dp0vibe.bat" shutdown
echo.
pause
