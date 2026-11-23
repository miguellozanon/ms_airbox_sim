@echo off
title Airbox MS9 - abriendo el panel
REM Panel de simulaciones del airbox: http://127.0.0.1:8780 (el Go2 usa 8765, el B2 8770, Performance Lab 8090).
REM Corre oculto con el Python de Isaac Sim (solo biblioteca estandar); el calculo va en WSL Ubuntu-24.04.
set "PY=C:\isaacsim\kit\python\python.exe"
if not exist "%PY%" set "PY=C:\Python314\python.exe"
if not exist "%PY%" set "PY=python"
curl.exe -s --max-time 2 http://127.0.0.1:8780/api/estado >nul 2>&1
if not errorlevel 1 goto abrir
echo Arrancando el panel de simulaciones...
powershell -NoProfile -Command "Start-Process -WindowStyle Hidden -FilePath '%PY%' -ArgumentList '-u','C:\ms_airbox\app\panel.py','--oculto','--arrancar-trabajador'"
for /l %%i in (1,1,30) do (
  timeout /t 1 /nobreak >nul
  curl.exe -s --max-time 1 http://127.0.0.1:8780/api/estado >nul 2>&1 && goto abrir
)
echo.
echo No se ha podido abrir el panel. Revisa C:\ms_airbox\cola\panel.log
pause
exit /b 1
:abrir
start "" http://127.0.0.1:8780
