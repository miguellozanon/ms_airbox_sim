@echo off
title Airbox MS9 - apagar
curl.exe -s --max-time 40 -X POST -H "X-Airbox: 1" -H "Content-Type: application/json" -d "{\"accion\":\"apagar\"}" http://127.0.0.1:8780/api/trabajador >nul 2>&1
if errorlevel 1 (echo El panel no estaba abierto.) else (echo Panel y calculo detenidos.)
timeout /t 3 >nul
