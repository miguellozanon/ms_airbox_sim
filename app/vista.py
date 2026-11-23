#!/usr/bin/env python3
"""Vistas 3D para el visor del panel: convierte el STEP de un caso (plenum + runner, el volumen de aire) en un STL
binario ligero en cola/vista/<caso>.stl (mm, coordenadas del estudio). Lo usa el trabajador cuando el panel lo pide
(cola/vista/peticiones/<caso>.req) y al terminar cada caso.  Uso: python vista.py caso1 [caso2 ...]
"""
import json, os, sys, time

ROOT = os.environ.get('AIRBOX_ROOT', os.path.dirname(os.path.abspath(__file__)))
QUEUE = os.environ.get('AIRBOX_QUEUE', '/mnt/c/ms_airbox/cola')
VISTA = os.path.join(QUEUE, 'vista')


def step_path(name):
    return os.path.join(ROOT, 'geoms', f'{name}_plenum_runner.step')


def job_params(name):
    import glob
    for d in ('hechos', 'fallidos', 'en_curso', 'pendientes'):
        for f in glob.glob(os.path.join(QUEUE, d, f'*_{name}.json')) + glob.glob(os.path.join(QUEUE, d, f'{name}.json')):
            try:
                with open(f, encoding='utf-8') as fh:
                    j = json.load(fh)
                j = j.get('job', j)
                if j.get('name') == name:
                    return j.get('params', {})
            except Exception:
                pass
    return None


def make(name, tol=0.08, ang=0.12):
    import numpy as np, trimesh, cadquery as cq
    os.makedirs(VISTA, exist_ok=True)
    out = os.path.join(VISTA, f'{name}.stl')
    st = step_path(name)
    if not os.path.exists(st):
        with open(out[:-4] + '.err', 'w', encoding='utf-8') as f:
            f.write('no se conserva la geometría de este caso (STEP no encontrado)')
        return False
    s = cq.importers.importStep(st).val()
    v, f = s.tessellate(tol, ang)
    m = trimesh.Trimesh(np.array([(p.x, p.y, p.z) for p in v], dtype=float), np.array(f), process=True)
    pr = job_params(name)
    if pr and any(k.startswith('c_') for k in pr):          # conducto modificado: se añade a la vista (el v5 lo pone el visor)
        import conducto
        if not conducto.is_neutral(pr):
            d = conducto.mesh(pr).slice_plane([-206.0, 0, 0], [-1, 0, 0], cap=False)
            m = trimesh.util.concatenate([m, d])
    tmp = out + '.tmp'
    m.export(tmp, file_type='stl')                      # binario
    os.replace(tmp, out)
    try:
        os.remove(out[:-4] + '.err')
    except OSError:
        pass
    return True


if __name__ == '__main__':
    for n in sys.argv[1:]:
        t0 = time.time()
        try:
            ok = make(n)
            print(n, 'vista', 'ok' if ok else 'sin STEP', f'{time.time() - t0:.1f} s', flush=True)
        except Exception as e:
            with open(os.path.join(VISTA, f'{n}.err'), 'w', encoding='utf-8') as f:
                f.write(f'{type(e).__name__}: {e}')
            print(n, 'vista ERROR', e, flush=True)
