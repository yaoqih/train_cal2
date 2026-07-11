@echo off
setlocal
cd /d "%~dp0"

if exist "server.env.cmd" call "server.env.cmd"

if not defined TRAIN_CAL_API_WORKERS set "TRAIN_CAL_API_WORKERS=1"
if not defined TRAIN_CAL_API_MAX_PENDING set "TRAIN_CAL_API_MAX_PENDING=4"

if not defined TRAIN_CAL_API_KEY (
  if /I not "%TRAIN_CAL_ALLOW_UNAUTHENTICATED%"=="true" (
    echo TRAIN_CAL_API_KEY is required.
    echo Copy server.env.example.cmd to server.env.cmd and set a private API key.
    exit /b 1
  )
)

"%~dp0train-cal-server.exe" %*
