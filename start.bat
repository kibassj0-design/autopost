@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
set "VPY=.venv\Scripts\python.exe"
set "CHECK=import sys,struct;sys.exit(0 if sys.version_info[:2]>=(3,10) and struct.calcsize('P')==8 else 1)"

rem Installed before and still healthy: go straight to the tool.
if not exist ".venv\installed.ok" goto setup
"%VPY%" -c "import anthropic, requests" >nul 2>&1
if errorlevel 1 goto setup
goto run

:setup
echo ============================================================
echo  Installing autopost. This runs once and takes a few minutes.
echo  Do not close this window.
echo ============================================================
if exist ".venv\installed.ok" del ".venv\installed.ok"

rem An existing environment built with a good Python can be reused.
if not exist "%VPY%" goto findpython
"%VPY%" -c "%CHECK%" >nul 2>&1
if errorlevel 1 goto rebuild
goto packages

:rebuild
echo Removing an old environment built with an unsupported Python...
rmdir /s /q .venv

:findpython
set "PY="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "py -3.10" "py -3" "python" "python3") do call :trypython %%~C
if defined PY goto makevenv

echo.
echo Python 3.10 or newer (64-bit) was not found. Installing Python 3.12...
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
set "NEWPY=%LocalAppData%\Programs\Python\Python312\python.exe"
if not exist "%NEWPY%" goto nopython
set PY="%NEWPY%"

:makevenv
echo.
echo Using this Python:
%PY% --version
%PY% -m venv .venv
if errorlevel 1 goto failed

:packages
echo.
echo Downloading and installing packages...
"%VPY%" -m pip install --upgrade pip
"%VPY%" -m pip install -r requirements.txt
if not errorlevel 1 goto installed

echo.
echo Installing everything together failed. Trying one package at a time...
set "SOMEFAILED="
for /f "usebackq delims=" %%P in ("requirements.txt") do call :pipone "%%P"
"%VPY%" -c "import anthropic, requests" >nul 2>&1
if errorlevel 1 goto failed
if not defined SOMEFAILED goto installed
echo.
echo WARNING: the packages marked FAILED above could not be installed.
echo The tool will start, but cutting clips from links may not work.
echo Send a screenshot of this window to get it fixed.
echo.
pause
goto run

:installed
echo ok> ".venv\installed.ok"
echo.
echo Installation finished.
echo.
goto run

:run
if not exist .env copy .env.example .env >nul
if not exist config.json copy config.example.json config.json >nul
"%VPY%" -m autopost %*
echo.
pause
exit /b 0

:nopython
echo.
echo Could not install Python automatically.
echo Install it from https://www.python.org/downloads/  (tick "Add python.exe to PATH"),
echo then double-click start.bat again.
echo.
pause
exit /b 1

:failed
echo.
echo ============================================================
echo  Installation FAILED. Check your internet connection and
echo  double-click start.bat again. If it fails again, send a
echo  screenshot of this window.
echo ============================================================
echo.
pause
exit /b 1

:trypython
if defined PY exit /b 0
%* -c "%CHECK%" >nul 2>&1
if not errorlevel 1 set "PY=%*"
exit /b 0

:pipone
"%VPY%" -m pip install "%~1"
if not errorlevel 1 exit /b 0
set "SOMEFAILED=1"
echo FAILED: "%~1"
exit /b 0
