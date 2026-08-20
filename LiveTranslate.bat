@echo off
REM LiveTranslate - always-on-top subtitles over whatever you are presenting.
REM Double-click to start.
REM
REM   Ctrl+Alt+S   show / hide the subtitles
REM   Ctrl+Alt+T   move them between the bottom and the top of the screen
REM   Ctrl+Alt+P   pause / resume recognition
REM
REM LM Studio on another machine:  LiveTranslate.bat --lmstudio 192.168.1.50
setlocal
cd /d "%~dp0"
call scripts\bootstrap.bat
if errorlevel 1 goto :failed
"%LT_PYTHON%" -m livetranslate --config config.windows.toml --overlay %*
if errorlevel 1 goto :failed
endlocal
exit /b 0

:failed
echo.
echo LiveTranslate exited with an error. See logs\livetranslate.log
echo.
pause
exit /b 1
