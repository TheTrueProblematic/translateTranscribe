@echo off
REM LiveTranslate - ARS training mode, with the ARS session vocabulary.
REM Same subtitle overlay as LiveTranslate.bat.
setlocal
cd /d "%~dp0"
call scripts\bootstrap.bat
if errorlevel 1 goto :failed
"%LT_PYTHON%" -m livetranslate --config config.windows.ars.toml --overlay %*
if errorlevel 1 goto :failed
endlocal
exit /b 0

:failed
echo.
echo LiveTranslate exited with an error. See logs\livetranslate.log
echo.
pause
exit /b 1
