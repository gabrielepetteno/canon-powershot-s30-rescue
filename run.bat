@echo off
REM RetroCam Rescue launcher (Windows).
REM
REM Prefers the "py" launcher over a bare "python", because py finds the newest
REM real installation while "python" is whatever happens to be first on PATH.
REM The probe imports tkinter, which conveniently also rejects the 0-byte
REM Microsoft Store alias in WindowsApps: that stub opens the Store instead of
REM starting Python, and exits non-zero.
REM
REM Override the interpreter with:  set RETROCAM_PYTHON=C:\path\to\python.exe
setlocal EnableExtensions
title RetroCam Rescue

set "APP_DIR=%~dp0"
set "ARGS=%*"

REM src layout: run straight from the checkout, no install step needed.
set "PYTHONPATH=%APP_DIR%src;%PYTHONPATH%"
set "FOUND="

if defined RETROCAM_PYTHON call :probe "%RETROCAM_PYTHON%"
call :probe py -3.14
call :probe py -3.13
call :probe py -3.12
call :probe py -3.11
call :probe py -3.10
call :probe py -3.9
call :probe py -3
call :probe python
call :probe python3

if not defined FOUND goto :nopython

REM The console window stays visible on purpose: for a data-rescue tool the log
REM is support evidence, and a crash with no window is a crash nobody can
REM report. Use the installed "retrocam" gui-script if you want it windowless.
%FOUND% -m retrocam %ARGS%
set "RC=%errorlevel%"
if not "%RC%"=="0" (
    echo.
    echo RetroCam Rescue exited with an error ^(code %RC%^).
    echo RetroCam Rescue e' uscito con un errore ^(codice %RC%^).
    echo.
    pause
)
exit /b %RC%

:probe
REM %* is the candidate command, e.g. "py -3.13". Exit code 0 means it is a
REM Python 3.9+ with a working tkinter.
if defined FOUND goto :eof
%* -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
if errorlevel 1 goto :eof
set "FOUND=%*"
goto :eof

:nopython
echo.
echo RetroCam Rescue: no usable Python 3.9+ with Tkinter was found.
echo RetroCam Rescue: nessun Python 3.9+ con Tkinter utilizzabile.
echo.
echo Install Python from https://www.python.org/downloads/windows/
echo   -^> during setup keep "tcl/tk and IDLE" CHECKED ^(that is Tkinter^)
echo   -^> and tick "Add python.exe to PATH"
echo Or, from a terminal:  winget install Python.Python.3.13
echo.
echo If typing "python" opens the Microsoft Store, that is the alias stub:
echo   Settings ^> Apps ^> Advanced app settings ^> App execution aliases
echo   -^> turn OFF python.exe and python3.exe
echo.
echo Then run this file again, or set RETROCAM_PYTHON to a python.exe path.
echo.
pause
exit /b 1
