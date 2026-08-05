@echo off
REM ---------------------------------------------------------------------
REM  Non-Steam Game Manager
REM
REM  Looks for a Python already on this machine. If there is none, it
REM  offers to drop a portable copy into this folder. That copy is not
REM  installed: nothing touches the registry or your PATH, no admin
REM  rights are needed, and deleting this folder removes every trace.
REM ---------------------------------------------------------------------
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Non-Steam Game Manager

set "PYVER=3.12.10"
set "RUNTIME=%~dp0runtime"
set "PYEXE="
set "PYARGS="

REM --- 1. a portable runtime left by an earlier run ---------------------
if exist "%RUNTIME%\python.exe" (
    set "PYEXE=%RUNTIME%\python.exe"
    goto :run
)

REM --- 2. a Python already installed ------------------------------------
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" >nul 2>&1
if !errorlevel! equ 0 (
    set "PYEXE=py"
    set "PYARGS=-3"
    goto :run
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" >nul 2>&1
if !errorlevel! equ 0 (
    set "PYEXE=python"
    goto :run
)

REM --- 3. offer to fetch a portable one ---------------------------------
echo.
echo   No Python was found on this computer.
echo.
echo   I can download a portable copy from python.org into this folder,
echo   about 11 MB. To be clear about what that means:
echo.
echo     - it is NOT installed, and runs only from this folder
echo     - nothing is added to your PATH or the registry
echo     - no administrator rights are needed
echo     - deleting this folder removes it completely
echo.
set "OK="
set /p "OK=  Download it now? [Y/n] "
if /i "!OK!"=="n" goto :manual

set "URL=https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-embed-amd64.zip"
set "TMPZIP=%TEMP%\nsgm-python-%PYVER%.zip"

echo.
echo   Downloading %URL%
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%URL%' -OutFile '%TMPZIP%' -UseBasicParsing"
if not exist "%TMPZIP%" goto :dlfail

echo   Unpacking into .\runtime
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%TMPZIP%' -DestinationPath '%RUNTIME%' -Force"
del "%TMPZIP%" >nul 2>&1
if not exist "%RUNTIME%\python.exe" goto :dlfail

REM The embeddable build fixes its own import path in a ._pth file, so the
REM app folder has to be added or "import nsgm" will not resolve.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -Path '%RUNTIME%\python*._pth' | ForEach-Object { if (-not (Select-String -Path $_.FullName -SimpleMatch '..\src' -Quiet)) { Add-Content -Path $_.FullName -Value '..\src' } }"

set "PYEXE=%RUNTIME%\python.exe"
echo   Ready.
echo.

REM --- run ---------------------------------------------------------------
:run
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
"%PYEXE%" %PYARGS% -m nsgm %*
if !errorlevel! neq 0 goto :runfail
goto :eof

:manual
echo.
echo   No problem. You can also install Python yourself from
echo   https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^)
echo   and then run this file again.
echo.
pause
goto :eof

:dlfail
echo.
echo   The download did not finish. This is usually a firewall, a proxy,
echo   or no internet connection at the moment.
echo.
echo   You can do the same thing by hand:
echo     1. Download %URL%
echo     2. Extract it into a folder called "runtime" next to this file
echo     3. Open runtime\python312._pth in Notepad and add a line: ..\src
echo     4. Run this file again
echo.
pause
goto :eof

:runfail
echo.
echo   The app stopped unexpectedly. The message above says why.
echo.
pause
