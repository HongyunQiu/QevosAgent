@echo off
setlocal EnableDelayedExpansion

REM ============================================================================
REM QevosAgent Dashboard launcher
REM   - Activates the conda env named in CONDA_ENV
REM   - Frees port PORT if an old node.exe is still listening on it
REM   - Starts: node dashboard\server.js
REM
REM Usage: double-click, or right-click -> Send to -> Desktop (create shortcut).
REM
REM NOTE: this file MUST be saved as ASCII (no CJK / box-drawing chars), because
REM       cmd.exe parses .bat files in the system OEM codepage (CP936 on most
REM       Chinese Windows). UTF-8 multibyte characters get mis-parsed as commands.
REM ============================================================================

REM ---- Configuration (edit if you move the repo or rename the env) ----------
set "REPO_DIR=E:\workspace\QevosAgent"
set "PORT=8765"
set "CONDA_ENV=nanoGPT"
REM ---------------------------------------------------------------------------

title QevosAgent Dashboard (port %PORT%, env %CONDA_ENV%)
echo ============================================================
echo  QevosAgent Dashboard launcher
echo    repo : %REPO_DIR%
echo    port : %PORT%
echo    env  : %CONDA_ENV%
echo ============================================================
echo.

if not exist "%REPO_DIR%\dashboard\server.js" (
    echo [ERROR] not found: %REPO_DIR%\dashboard\server.js
    echo         Edit REPO_DIR at the top of this .bat
    pause
    exit /b 1
)

cd /d "%REPO_DIR%"


REM ---- [1/4] Activate conda env ---------------------------------------------
echo [1/4] Activating conda env "%CONDA_ENV%"...

set "CONDA_OK="

REM Path 1: conda already on PATH (user did "conda init cmd.exe")
where conda >nul 2>&1
if not errorlevel 1 (
    call conda activate %CONDA_ENV% 2>nul
    if not errorlevel 1 set "CONDA_OK=1"
)

REM Path 2: search common install locations for condabin\conda.bat
if not defined CONDA_OK (
    for %%P in (
        "%USERPROFILE%\anaconda3\condabin\conda.bat"
        "%USERPROFILE%\miniconda3\condabin\conda.bat"
        "%USERPROFILE%\Anaconda3\condabin\conda.bat"
        "%USERPROFILE%\Miniconda3\condabin\conda.bat"
        "%LOCALAPPDATA%\anaconda3\condabin\conda.bat"
        "%LOCALAPPDATA%\miniconda3\condabin\conda.bat"
        "%ProgramData%\anaconda3\condabin\conda.bat"
        "%ProgramData%\Anaconda3\condabin\conda.bat"
        "%ProgramData%\miniconda3\condabin\conda.bat"
        "%ProgramData%\Miniconda3\condabin\conda.bat"
        "C:\ProgramData\anaconda3\condabin\conda.bat"
        "C:\ProgramData\miniconda3\condabin\conda.bat"
    ) do (
        if not defined CONDA_OK if exist %%P (
            echo       found: %%~P
            call %%P activate %CONDA_ENV%
            if not errorlevel 1 set "CONDA_OK=1"
        )
    )
)

if not defined CONDA_OK (
    echo.
    echo       [ERROR] could not activate "%CONDA_ENV%".
    echo       Fix: either run "conda init cmd.exe" once, or add your
    echo            condabin\conda.bat path to the search list above.
    echo.
    pause
    exit /b 1
)
echo       [OK] env "%CONDA_ENV%" active
echo.


REM ---- [2/4] Check port -----------------------------------------------------
echo [2/4] Checking port %PORT%...

set "FOUND_PID="
for /f "tokens=5" %%P in ('netstat -ano -p TCP ^| findstr /R /C:":%PORT% .*LISTENING"') do (
    if not defined FOUND_PID set "FOUND_PID=%%P"
)

if defined FOUND_PID (
    echo       port %PORT% in use by PID !FOUND_PID!, identifying process...

    set "PROC_NAME="
    for /f "tokens=1 delims=," %%N in ('tasklist /FI "PID eq !FOUND_PID!" /FO CSV /NH 2^>nul') do (
        set "PROC_NAME=%%~N"
    )

    if /I "!PROC_NAME!"=="node.exe" (
        echo       owner is node.exe ^(PID !FOUND_PID!^), treating as old dashboard, killing...
        taskkill /F /PID !FOUND_PID! >nul 2>&1
        if errorlevel 1 (
            echo       [WARN] taskkill failed -- admin needed? trying anyway
        ) else (
            echo       [OK] old instance killed
        )

        REM Wait up to 5s for the socket to be released
        set /a _wait=0
        :wait_port
        timeout /t 1 /nobreak >nul
        set "STILL="
        for /f "tokens=5" %%P in ('netstat -ano -p TCP ^| findstr /R /C:":%PORT% .*LISTENING"') do (
            if not defined STILL set "STILL=%%P"
        )
        if defined STILL (
            set /a _wait+=1
            if !_wait! lss 5 goto :wait_port
            echo       [WARN] port still busy, starting anyway -- node may error
        )
    ) else (
        echo.
        echo       [ERROR] port %PORT% owned by non-node process: !PROC_NAME! ^(PID !FOUND_PID!^)
        echo               Refusing to auto-kill. Please handle manually:
        echo                 taskkill /F /PID !FOUND_PID!
        echo.
        pause
        exit /b 1
    )
) else (
    echo       port is free
)
echo.


REM ---- [3/4] Check node -----------------------------------------------------
echo [3/4] Checking node...
where node >nul 2>&1
if errorlevel 1 (
    echo       [ERROR] node not on PATH. Install Node.js or activate the right env.
    pause
    exit /b 1
)
for /f "delims=" %%V in ('node --version 2^>nul') do echo       node %%V
echo.


REM ---- [4/4] Start dashboard ------------------------------------------------
echo [4/4] Starting dashboard...
echo       Open in browser: http://127.0.0.1:%PORT%
echo       Press Ctrl+C to stop.
echo ============================================================
echo.

node dashboard\server.js

echo.
echo ============================================================
echo  Dashboard exited.
echo ============================================================
pause
