@echo off
title Airbox MS9 - instalacion
echo ============================================================
echo   Airbox MS9 - instalacion de la cadena de simulacion
echo ============================================================
echo.
where wsl.exe >nul 2>&1 || (echo No encuentro WSL. & pause & exit /b 1)
wsl.exe -d Ubuntu-24.04 -- true >nul 2>&1 || (echo No encuentro la distribucion Ubuntu-24.04 de WSL. & pause & exit /b 1)
echo Se instalaran OpenFOAM y un entorno de Python propio dentro de WSL Ubuntu-24.04.
echo No toca ROS 2 ni su Python. Te pedira la contrasena de tu usuario de Ubuntu.
echo.
wsl.exe -d Ubuntu-24.04 -- bash /mnt/c/ms_airbox/app/instalar_wsl.sh
if errorlevel 1 (echo. & echo ERROR durante la instalacion. Copia lo de arriba y pasaselo a Claude. & pause & exit /b 1)
echo.
echo Creando el acceso directo "Airbox MS9" en C:\ms_airbox ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('C:\ms_airbox\Airbox MS9.lnk'); $s.TargetPath='C:\ms_airbox\Abrir_panel.bat'; $s.WorkingDirectory='C:\ms_airbox'; $s.WindowStyle=7; $s.IconLocation='%SystemRoot%\System32\shell32.dll,12'; $s.Description='Panel de simulaciones del airbox'; $s.Save()"
echo.
echo Instalacion terminada. Abre el panel con "Airbox MS9" en C:\ms_airbox.
pause
