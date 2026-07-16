@echo off
setlocal
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "%~dp0webspider.py" %*
    exit /b %errorlevel%
)
where python >nul 2>nul
if %errorlevel%==0 (
    python "%~dp0webspider.py" %*
    exit /b %errorlevel%
)
echo Python 3 was not found. Install Python 3 and enable the Python launcher or add python.exe to PATH.
exit /b 9009
