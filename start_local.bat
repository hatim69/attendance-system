@echo off
rem Starts the attendance system on this PC (dashboard on http://localhost:5005, fingerprint device listener on port 80).
rem   start_local.bat            normal start
rem   start_local.bat log        also save every scan to punches_log.csv (safety net while gathering data)
rem   start_local.bat update     first download the latest code and packages
rem   start_local.bat update log both
cd /d "%~dp0"
call .venv\Scripts\activate
:args
if "%~1"=="" goto run
if /i "%~1"=="log" set PUNCH_LOG_FILE=punches_log.csv
if /i "%~1"=="update" (
  git pull
  pip install -r requirements.txt
)
shift
goto args
:run
if defined PUNCH_LOG_FILE echo Saving every scan to %PUNCH_LOG_FILE%
echo Dashboard: http://localhost:5005   (leave this window open; Ctrl+C stops it)
uvicorn app.main:app --host 0.0.0.0 --port 5005
