@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if exist "%PYTHON%" goto ready
for /f "delims=" %%P in ('py -3 -c "import sys; assert min(sys.version_info[:2], (3,10)) == (3,10); print(sys.executable)" 2^>nul') do set "PYTHON=%%P"
if exist "%PYTHON%" goto ready
for /f "delims=" %%P in ('python -c "import sys; assert min(sys.version_info[:2], (3,10)) == (3,10); print(sys.executable)" 2^>nul') do set "PYTHON=%%P"
if exist "%PYTHON%" goto ready
echo Python 3.12 is required. Install it using Windows Package Manager?
choice /C YN /N /M "Authorize installation? [Y/N] "
if errorlevel 2 goto finished
where winget >nul 2>nul
if errorlevel 1 (
    echo Windows Package Manager is unavailable. An administrator must provision Python 3.12.
    goto finished
)
winget install --id Python.Python.3.12 --exact --scope user --accept-source-agreements --accept-package-agreements
if errorlevel 1 goto finished
set "PYTHON=%LocalAppData%\Programs\Python\Python312\python.exe"
if not exist "%PYTHON%" (
    echo Open Start.cmd again after Windows refreshes PATH.
    goto finished
)
:ready
set "PYTHONUTF8=1"
"%PYTHON%" -B "%~dp0reborn.py" onboard
:finished
echo.
pause
