@echo off
REM LiveTranslate - full-screen browser display, the same way the Mac runs.
REM Use this when the audience reads from a projector rather than from an
REM overlay on top of a presentation.
setlocal
cd /d "%~dp0"
call scripts\bootstrap.bat
if errorlevel 1 goto :failed
"%LT_PYTHON%" -m livetranslate --config config.windows.toml %*
if errorlevel 1 goto :failed
endlocal
exit /b 0

:failed
echo.
echo LiveTranslate exited with an error. See logs\livetranslate.log
echo.
pause
exit /b 1
