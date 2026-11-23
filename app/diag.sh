#!/bin/bash
# Diagnóstico de solo lectura del trabajador (lo lanza el panel: GET /api/diag)
R="$HOME/ms_airbox"
echo "== $(date '+%F %T')  $(hostname)"
echo "== disco"; df -h "$HOME" /mnt/c 2>&1
echo "== memoria"; free -m
echo "== tamaño"; du -sh "$R/cases" "$R/geoms" 2>/dev/null; echo "casos en disco: $(ls "$R/cases" 2>/dev/null | wc -l)"
echo "== procesos (memoria)"; ps -eo pid,rss,etime,comm --sort=-rss | head -12
echo "== límites"; ulimit -n; cat /proc/sys/fs/file-nr
L=$(ls -td "$R"/cases/*/ 2>/dev/null | head -1)
echo "== caso más reciente: $L"
for f in log.blockMesh log.recon log.recon0 log.reconstructPar log.decomposePar log.snappy; do
  [ -f "$L/$f" ] && { echo "--- $f (final)"; tail -25 "$L/$f"; }
done
echo "== ros"; pgrep -fa "ros2|rviz|gazebo|isaac" | head -10
echo "== OpenFOAM"; for c in blockMesh simpleFoam snappyHexMesh reconstructPar decomposePar; do printf '%s: ' $c; command -v $c || echo 'NO ENCONTRADO'; done
ls -la /usr/bin/blockMesh /usr/bin/simpleFoam 2>&1 | head; ls -d /usr/lib/openfoam/* 2>/dev/null | head
echo "--- apt/dpkg recientes"; grep -h -i -E "openfoam|upgrade" /var/log/apt/history.log 2>/dev/null | tail -8; grep -h -i openfoam /var/log/dpkg.log 2>/dev/null | tail -8
echo "--- unattended"; tail -5 /var/log/unattended-upgrades/unattended-upgrades.log 2>/dev/null
echo "== búsqueda de binarios"; ls -la /usr/lib/openfoam/ 2>&1; find /usr/lib/openfoam /usr/lib/x86_64-linux-gnu/openfoam* /usr/share/openfoam* -maxdepth 6 -name blockMesh 2>/dev/null | head
ls /usr/lib/openfoam/openfoam/etc/bashrc /usr/lib/openfoam/openfoam2606/etc/bashrc 2>&1
dpkg -l | grep -i openfoam | awk '{print $1,$2,$3}'
apt-cache policy openfoam 2>/dev/null | head -12
grep -rh "openfoam" /etc/apt/sources.list.d/ 2>/dev/null | head -3
echo "--- historial completo openfoam"; grep -h -B3 -A1 "openfoam" /var/log/apt/history.log 2>/dev/null | grep -E "Start-Date|Commandline|Requested-By" | tail -6
sudo -n true 2>/dev/null && echo "sudo sin contraseña: sí" || echo "sudo sin contraseña: no"
echo "== OpenFOAM v1912 privado"; OF=$HOME/openfoam1912; ls -la $OF/usr/bin/blockMesh 2>&1; ls $OF/deb 2>&1; ls -la /usr/lib/libOpenFOAM.so /usr/lib/libPstream.so /lib/libPstream.so 2>&1
PATH=$OF/usr/bin:$PATH WM_PROJECT_DIR=$OF/usr/share/openfoam FOAM_ETC=$OF/usr/share/openfoam/etc simpleFoam -help 2>&1 | head -3
