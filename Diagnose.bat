@echo off
REM Tests each stage in the order it can fail, then waits so the results can
REM be read. Run this first when something is not working.
setlocal
cd /d "%~dp0"
call scripts\bootstrap.bat
if errorlevel 1 goto :failed
"%LT_PYTHON%" -m livetranslate --config config.windows.toml --diagnose %*
echo.
pause
endlocal
exit /b 0

:failed
echo.
echo Could not start. Python or the dependencies are not installed correctly.
echo.
pause
exit /b 1
