#!/usr/bin/env bash
# Instala la parte Linux de la cadena (OpenFOAM + Python) en WSL Ubuntu 24.04.
# Lo ejecuta C:\ms_airbox\Instalar.bat; no hace falta abrir Ubuntu.
set -euo pipefail
VENV="$HOME/airbox_env"
DEST="$HOME/ms_airbox"

echo "== 1/3 Paquetes del sistema (pedirá la contraseña de tu usuario de Ubuntu) =="
sudo apt-get update
sudo apt-get install -y openfoam openmpi-bin python3-venv python3-pip curl \
     libgl1 libglu1-mesa libxrender1 libxcursor1 libxft2 libxinerama1

echo "== 2/3 Entorno de Python (CadQuery, gmsh, trimesh...) =="
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$(dirname "${BASH_SOURCE[0]}")/requirements.txt"

echo "== 3/3 Comprobaciones =="
mkdir -p "$DEST"
"$VENV/bin/python" -c "import cadquery, gmsh, trimesh, shapely, manifold3d; trimesh.creation.box().slice_plane([0,0,0],[1,0,0],cap=True); print('Python OK')"
for c in simpleFoam snappyHexMesh blockMesh mpirun; do command -v $c >/dev/null || { echo "Falta $c"; exit 1; }; done
echo "OpenFOAM OK"
echo "Hilos de CPU: $(nproc)   Memoria: $(free -g | awk '/Mem/{print $2}') GB"
