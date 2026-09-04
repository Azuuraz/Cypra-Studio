@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Cypra Matrix Studio - Offline Setup
color 0A
cls
set "ROOT=%~dp0"
set "APP=%~1"
if "%APP%"=="" set "APP=%ROOT%.."
for %%I in ("%APP%") do set "APP=%%~fI"
set "PY=%LocalAppData%\Programs\Python\Python312\python.exe"
set "VENV=%APP%\.venv"
set "PYINSTALLER=%ROOT%python-3.12.10-amd64.exe"
echo.
echo  ================================================================
echo       CYPRA MATRIX STUDIO ^| OFFLINE ENVIRONMENT SETUP
echo  ================================================================
echo  Target: %APP%
echo  Mode:   LOCAL FILES ONLY - NO INTERNET REQUIRED
echo.
call :stage 1 4 "Checking MatrixStudio target"
if not exist "%APP%\app.py" goto :fail_target
echo       [OK] MatrixStudio application found
if exist "%PY%" goto :python_ready
call :stage 2 4 "Installing Python 3.12"
if not exist "%PYINSTALLER%" goto :fail_python_file
echo       [..] Running bundled installer (offline)...
start /wait "Python 3.12" "%PYINSTALLER%" /quiet InstallAllUsers=0 Include_launcher=0 Include_pip=1 PrependPath=0 Include_test=0
if not exist "%PY%" goto :fail_python
echo       [OK] Python 3.12 ready
goto :python_done
:python_ready
call :stage 2 4 "Checking Python 3.12"
echo       [OK] Existing Python runtime found
:python_done
call :stage 3 4 "Preparing MatrixStudio virtual environment"
if exist "%VENV%\Scripts\python.exe" goto :venv_ready
echo       [..] Creating .venv in the MatrixStudio folder...
"%PY%" -m venv "%VENV%"
:venv_ready
if not exist "%VENV%\Scripts\python.exe" goto :fail_venv
echo       [OK] %VENV%\Scripts\python.exe
call :stage 4 4 "Loading offline Python packages"
if not exist "%ROOT%python_packages\fastapi" goto :fail_cache
for /f %%A in ('dir /b /ad "%ROOT%python_packages" 2^>nul ^| find /c /v ""') do set "PKGCOUNT=%%A"
echo       [..] Copying !PKGCOUNT! cached package folders...
robocopy "%ROOT%python_packages" "%VENV%\Lib\site-packages" /E /NFL /NDL /NJH /NJS /NP >nul
if %ERRORLEVEL% GTR 7 goto :fail_copy
echo       [OK] Offline package cache copied
echo       [..] Validating runtime imports...
"%VENV%\Scripts\python.exe" -c "import fastapi,uvicorn,openai,requests,httpx,multipart,pydantic,PIL,webview,winpty" >nul 2>&1
if errorlevel 1 goto :fail_imports
echo       [OK] FastAPI, Uvicorn, OpenAI, HTTPX, Pillow, WebView2 bridge
echo.
echo  ================================================================
echo       [SUCCESS] OFFLINE SETUP COMPLETE
echo  ================================================================
echo  Environment: %VENV%
echo  Next step:   run %APP%\START.bat
echo.
pause
exit /b 0

:stage
echo.
echo  [STAGE %~1/%~2] %~3
exit /b 0
:fail_target
echo       [FAIL] MatrixStudio app.py was not found.
goto :fail
:fail_python_file
echo       [FAIL] Bundled python-3.12.10-amd64.exe is missing.
goto :fail
:fail_python
echo       [FAIL] Python installer did not create the expected runtime.
echo       Check the installer log in %TEMP%.
goto :fail
:fail_venv
echo       [FAIL] Could not create MatrixStudio\.venv.
goto :fail
:fail_cache
echo       [FAIL] Setup\python_packages is empty or incomplete.
goto :fail
:fail_copy
echo       [FAIL] Offline package copy failed.
goto :fail
:fail_imports
echo       [FAIL] Runtime import validation failed.
goto :fail
:fail
echo.
echo  ================================================================
echo       [FAILED] OFFLINE SETUP STOPPED
echo  ================================================================
echo  Correct the item above, then run OFFLINE_SETUP.bat again.
echo.
pause
exit /b 1
