@echo off
setlocal

title DHANA DHANYA KADAI – Billing System v7
color 0A

:: 🔥 Use Python launcher (auto picks correct version)
set PYTHON=py -3.10

echo.
echo  ============================================
echo   DHANA DHANYA KADAI – Billing System v7
echo  ============================================
echo.

:: ── Check Python ─────────────────────────────────
%PYTHON% --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.10 not found.
    echo Installed versions:
    py -0
    pause
    exit /b 1
)

:: ── Install dependencies ONLY if something is missing ──────────
:: (Previously pip ran on every launch, which needs internet and slows the
::  store PC's startup. Now it runs only on first install / after breakage.)
echo [1/3] Checking dependencies...
%PYTHON% -c "import flask, flask_cors, openpyxl, webview, escpos, usb, wcwidth, uharfbuzz, freetype, PIL, reportlab" >nul 2>&1
if errorlevel 1 (
    echo [2/3] Installing missing dependencies...
    %PYTHON% -m pip install -r "%~dp0backend\requirements.txt"
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed. Check the internet connection and retry.
        pause
        exit /b 1
    )
) else (
    echo [2/3] Dependencies OK.
)

:: ── Run app ─────────────────────────────────────
echo [3/3] Launching...
cd /d "%~dp0"

:: Use pythonw (windowless) so no terminal appears during normal use.
:: On failure the window closes immediately — keep the py fallback for
:: debugging by running:  py -3.10 desktop_app.py
set PYTHONW=pythonw
py -3.10 -c "import sys; open('_pyver.txt','w').write(sys.executable)" >nul 2>&1
for /f "delims=" %%P in ('py -3.10 -c "import sys,os; print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"') do set PYTHONW=%%P

start "" "%PYTHONW%" "%~dp0desktop_app.py"

:: No pause — the bat closes immediately, app runs silently in background.
exit /b 0