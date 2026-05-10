@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%desktop" || (
  echo [ERROR] Unable to enter desktop directory.
  exit /b 1
)

echo [1/2] Running npm run setup...
call npm run setup
if errorlevel 1 (
  echo [ERROR] npm run setup failed.
  popd
  exit /b 1
)

echo [2/2] Running npm run build...
call npm run build
if errorlevel 1 (
  echo [ERROR] npm run build failed.
  popd
  exit /b 1
)

echo [OK] Windows installer build completed.
popd
exit /b 0
