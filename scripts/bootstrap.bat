@echo off
REM Shared startup for the LiveTranslate .bat launchers.
REM Creates the virtual environment and installs dependencies on first run;
REM later runs skip straight to starting. Kept in one file so the launchers
REM cannot drift apart.
REM
REM Deliberately written with goto rather than parenthesised if-blocks: cmd.exe
REM expands %VAR% for a whole block when it parses it, so a variable set by a
REM subroutine inside a block reads as empty on the next line of that block.

set "VENV=%~dp0..\.venv"
set "STAMP=%VENV%\.deps-installed"
set "REQS=%~dp0..\requirements-windows.txt"

if exist "%VENV%\Scripts\python.exe" goto :have_venv

echo First run: creating the environment...
call :find_python
if errorlevel 1 exit /b 1
"%PYEXE%" -m venv "%VENV%"
if errorlevel 1 goto :venv_failed
if not exist "%VENV%\Scripts\python.exe" goto :venv_failed

:have_venv
if exist "%STAMP%" goto :have_deps

echo First run: installing dependencies. This downloads a few hundred MB and
echo takes several minutes. It only happens once.
echo.
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip --quiet
"%VENV%\Scripts\python.exe" -m pip install --timeout 60 --retries 20 -r "%REQS%"
if errorlevel 1 goto :deps_failed
echo installed> "%STAMP%"

:have_deps
set "LT_PYTHON=%VENV%\Scripts\python.exe"
exit /b 0

:venv_failed
echo.
echo Could not create the virtual environment in %VENV%
exit /b 1

:deps_failed
echo.
echo Dependency installation failed. Check the network connection and run
echo this again. Nothing is lost; it resumes where it left off.
exit /b 1

:find_python
REM The py launcher ships with the python.org installer and is the most
REM reliable way to find a specific version.
for %%V in (3.12 3.11 3.13 3.10) do call :try_py %%V
if defined PYEXE exit /b 0
for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "PYEXE=%%P"
if defined PYEXE exit /b 0
echo.
echo Python 3.10 or newer was not found.
echo Install it from https://www.python.org/downloads/windows/
echo and tick "Add python.exe to PATH" during setup.
exit /b 1

:try_py
if defined PYEXE exit /b 0
for /f "delims=" %%P in ('py -%1 -c "import sys; print(sys.executable)" 2^>nul') do set "PYEXE=%%P"
exit /b 0
