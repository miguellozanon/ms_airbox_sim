#!/usr/bin/env bash
# Detiene de inmediato el trabajador del airbox y todo lo que ha lanzado (arranque, prueba de MPI, geometría,
# malla, mpirun, OpenFOAM). Solo toca ese árbol de procesos: nunca ROS 2, Isaac Sim ni nada más.
roots=$(pgrep -f "airbox_env/bin/python lanzador.py"; pgrep -f "bash .*/app/arrancar_trabajador.sh")
[ -z "$roots" ] && { echo "no estaba en marcha"; exit 0; }
tree() { echo "$1"; for c in $(ps -o pid= --ppid "$1"); do tree "$c"; done; }
pids=""; for r in $roots; do pids="$pids $(tree "$r")"; done
kill -TERM $pids 2>/dev/null
sleep 4
kill -KILL $pids 2>/dev/null
echo "detenidos: $(echo $pids | wc -w) procesos"
