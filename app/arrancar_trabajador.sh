#!/usr/bin/env bash
# Arranca el trabajador de la cola en WSL. Lo lanza el panel de Windows (oculto) con:
#   wsl -d Ubuntu-24.04 -- bash /mnt/c/ms_airbox/app/arrancar_trabajador.sh
# Prioridad mínima (nice 19) y grupo de procesos propio: así ROS 2 va antes y "Detener" solo toca lo nuestro.
APP="${AIRBOX_APP:-/mnt/c/ms_airbox/app}"
export AIRBOX_ROOT="$HOME/ms_airbox"                       # casos en el disco de Linux (rápido)
export AIRBOX_QUEUE="${AIRBOX_QUEUE:-/mnt/c/ms_airbox/cola}"
PY="$HOME/airbox_env/bin/python"
mkdir -p "$AIRBOX_QUEUE" "$AIRBOX_ROOT"
exec >>"$AIRBOX_QUEUE/trabajador.log" 2>&1
echo "---- $(date '+%Y-%m-%d %H:%M:%S') arranque del trabajador"
if [ ! -x "$PY" ]; then echo "ERROR: falta la instalación. Ejecuta C:\\ms_airbox\\Instalar.bat"; exit 1; fi
if pgrep -f "airbox_env/bin/python lanzador.py" >/dev/null; then echo "Ya estaba en marcha."; exit 0; fi
# Librerías: si app/requirements.txt ha cambiado desde la última vez, instala lo que falte
REQ="$APP/requirements.txt"; STAMP="$AIRBOX_ROOT/.requisitos_instalados"
if [ -f "$REQ" ] && ! cmp -s "$REQ" "$STAMP"; then
  echo "Instalando o actualizando librerías de Python (solo esta vez, 1-3 min)..."
  if "$HOME/airbox_env/bin/pip" install -q --disable-pip-version-check -r "$REQ"; then cp "$REQ" "$STAMP"; echo "Librerías OK"
  else echo "ERROR instalando librerías (¿sin internet?)"; fi
fi
# OpenFOAM: todo el estudio se ha calculado con OpenFOAM v1912 (paquete de Ubuntu). El 22/11 otra sesión cambió el
# paquete del sistema a v2606, así que el trabajador usa una copia privada de los binarios v1912 en ~/openfoam1912
# (descargada con apt-get download, sin sudo y sin tocar el sistema). Las librerías v1912 siguen instaladas
# (paquete libopenfoam); si faltaran, también se extraen en la copia privada.
OF="$HOME/openfoam1912"; OFV="1912.200626-2build3"
if [ ! -x "$OF/usr/bin/blockMesh" ]; then
  echo "Preparando OpenFOAM v1912 privado en $OF (solo la primera vez)..."
  mkdir -p "$OF/deb" && ( cd "$OF/deb" && apt-get download "openfoam=$OFV" "libopenfoam=$OFV" ) \
    && dpkg-deb -x "$OF/deb/openfoam_${OFV}_amd64.deb" "$OF" && echo "OpenFOAM v1912 privado listo" \
    || echo "ERROR preparando OpenFOAM v1912 privado"
fi
if [ ! -e /usr/lib/libOpenFOAM.so ] && [ -f "$OF/deb/libopenfoam_${OFV}_amd64.deb" ]; then
  [ -e "$OF/usr/lib/libOpenFOAM.so" ] || dpkg-deb -x "$OF/deb/libopenfoam_${OFV}_amd64.deb" "$OF"
  [ -e "$OF/usr/lib/libPstream.so" ] || ln -s openmpi-system/libPstream.so "$OF/usr/lib/libPstream.so"
  export LD_LIBRARY_PATH="$OF/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
export PATH="$OF/usr/bin:$PATH" WM_PROJECT_DIR="$OF/usr/share/openfoam" FOAM_ETC="$OF/usr/share/openfoam/etc"
if ! command -v blockMesh >/dev/null || [ "$(command -v blockMesh)" != "$OF/usr/bin/blockMesh" ]; then
  echo "ERROR: no se encuentra blockMesh v1912 ($(command -v blockMesh)). El trabajador no arranca."; exit 1
fi
echo "OpenFOAM: $(command -v simpleFoam)"
# OpenMPI en WSL: solo memoria compartida y la interfaz local, sin fijar procesos a núcleos
export OMPI_MCA_btl=self,vader OMPI_MCA_btl_tcp_if_include=lo OMPI_MCA_oob_tcp_if_include=lo
export OMPI_MCA_hwloc_base_binding_policy=none OMPI_MCA_rmaps_base_oversubscribe=1
export OMPI_ALLOW_RUN_AS_ROOT=1 OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1
# Prueba de MPI (a fichero, nunca por tubería: si se cuelga, no bloquea el arranque). Se prueban varias
# configuraciones y se usa la primera que funcione; si ninguna, se calcula en serie. Detalle: cola/mpi_test.log
MT="$AIRBOX_QUEUE/mpi_test.log"
echo "== $(date '+%Y-%m-%d %H:%M:%S') prueba de MPI (mpirun -np 2 hostname)" >"$MT"
MPI_ENV=""; AIRBOX_MPI=0
HW="HWLOC_COMPONENTS=-gl,-cuda,-nvml,-opencl,-levelzero,-rsmi"
for v in "$HW" "$HW PMIX_MCA_gds=hash" "-"; do
  [ "$v" = "-" ] && v=""
  echo "-- con: ${v:-(configuración por defecto)}" >>"$MT"
  env $v timeout -k 2 15 mpirun -np 2 hostname >>"$MT" 2>&1 </dev/null; rc=$?
  pkill -u "$(id -u)" -x orted 2>/dev/null
  echo "   código $rc" >>"$MT"
  if [ $rc -eq 0 ]; then AIRBOX_MPI=1; MPI_ENV="$v"; break; fi
done
export AIRBOX_MPI
if [ "$AIRBOX_MPI" = 1 ]; then
  [ -n "$MPI_ENV" ] && export $MPI_ENV
  echo "MPI OK (${MPI_ENV:-configuración por defecto})"
else
  echo "MPI no funciona con ninguna configuración: se calculará en serie. Diagnóstico en cola/mpi_test.log"
  {
    echo "== ompi_info"; ompi_info --version 2>&1 | head -2; dpkg -l | grep -E 'libpmix|openmpi-bin' | awk '{print $2, $3}'
    echo "== hostname: $(hostname)"; getent hosts "$(hostname)"
    echo "== /etc/hosts"; cat /etc/hosts; echo "== /etc/wsl.conf"; cat /etc/wsl.conf 2>/dev/null
    echo "== interfaces"; ip -o -4 addr 2>&1; echo "== /dev/shm"; df -h /dev/shm 2>&1
    echo "== variables"; env | grep -E '^(OMPI|PMIX|OPAL|HWLOC)_' | sort
    echo "== extensiones de hwloc"; ls /usr/lib/x86_64-linux-gnu/hwloc/ 2>&1
    echo "== dónde se queda esperando mpirun (8 s después de lanzarlo)"
    mpirun -np 1 hostname >"$MT.tmp" 2>&1 </dev/null & mp=$!
    sleep 8
    for p in $mp $(pgrep -P $mp); do
      echo "-- pid $p: $(tr '\0' ' ' </proc/$p/cmdline 2>/dev/null)  estado: $(awk '/^State/{print $2,$3}' /proc/$p/status 2>/dev/null)"
      for t in /proc/$p/task/*; do echo "   hilo ${t##*/}: wchan=$(cat $t/wchan 2>/dev/null) syscall=$(cut -d' ' -f1 $t/syscall 2>/dev/null)"; done
      echo "   ficheros abiertos:"; ls -l /proc/$p/fd 2>/dev/null | awk 'NR>1{print "     "$9,$10,$11}' | head -40
    done
    echo "-- salida hasta ahora:"; head -40 "$MT.tmp"
    kill -9 $mp $(pgrep -P $mp) 2>/dev/null; rm -f "$MT.tmp"
  } >>"$MT" 2>&1
fi
# Comprobación rápida antes de calcular
if ! "$PY" -c "import cadquery, gmsh, trimesh, shapely, manifold3d, numpy; m = trimesh.creation.box(); m.slice_plane([0, 0, 0], [1, 0, 0], cap=True)" ; then
  echo "ERROR: faltan librerías o no funcionan (ver arriba). El trabajador no arranca."; exit 1
fi
# Copia el código de Windows al disco de Linux: basta con actualizar C:\ms_airbox\app
for f in geom.py runcase.py ofpost.py metrics.py lanzador.py doe.py optim.py estudio.py vista.py conducto.py; do cp "$APP/$f" "$AIRBOX_ROOT/"; done
rm -rf "$AIRBOX_ROOT/template"; cp -r "$APP/template" "$APP/data" "$AIRBOX_ROOT/"
cd "$AIRBOX_ROOT"
exec setsid -w nice -n 19 "$PY" lanzador.py
