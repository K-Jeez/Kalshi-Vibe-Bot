@echo off
setlocal
cd /d "%~dp0"
echo.
echo Starting Kalshi Vibe Bot.
echo The trading state stays Stop until you press Play in the UI or run:  vibe.bat bot play
echo.
call "%~dp0vibe.bat" launch --open-browser
echo.
if errorlevel 1 echo Launch did not finish. Play was not turned on.
pause
