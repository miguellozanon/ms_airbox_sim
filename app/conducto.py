#!/usr/bin/env python3
"""Conducto de entrada parametrizado (toma RAM -> plenum) a partir del v5.

Se deforma la malla del v5 (data/duct_v5_mm.stl, coordenadas del estudio, mm) sin cambiar su topología, así que la
boca oval de la toma y el paso por el chasis quedan EXACTAMENTE como en el v5:

  tramo A  x < -770        boca oval y primer tramo (encaja con el carenado frontal): fijo
  tramo B  -770 .. -580    entre la toma y el chasis: abombado/estrechado de la sección   (c_sB)
  tramo C  -580 .. -405    paso por la pipa del chasis, partido en dos por el eje de dirección: fijo
  tramo D  -405 .. -205    del chasis al plenum: subida del «valle» del eje (c_dy) y difusor
                            hacia la boca del plenum, ancho (c_sw) y alto (c_sh)

Restricción de holgura (data/conducto_aux.npz: nube de puntos de chasis, eje de dirección, motor, carenado frontal,
tapa «Cubre» y, barridas por el giro de la dirección hasta ±GIRO grados —25° según el equipo, 21/11—, horquilla y tijas): cada punto de la
superficie que se mueva hacia fuera debe quedar a >= HOLGURA mm de cualquier pieza, o no acercarse más que en el v5
si allí el v5 ya estaba por debajo. Se comprueba de forma conservadora: d_nuevo >= d_v5 - desplazamiento.
"""
import math, os
import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
VARS = [('c_sB', 0.85, 1.30), ('c_dy', 0.0, 15.0), ('c_sw', 0.85, 1.50), ('c_sh', 0.90, 1.08)]
NEUTRO = {'c_sB': 1.0, 'c_dy': 0.0, 'c_sw': 1.0, 'c_sh': 1.0}
HOLGURA = 10.0          # mm desde la superficie de aire (≈ 3 mm de pared + 7 mm de juego), igual que en el plenum
XA, XB1, XC0, XC1, XD1 = -770.0, -580.0, -580.0, -405.0, -215.0
_C = {}


def _load():
    if _C:
        return _C
    import trimesh
    from scipy.spatial import cKDTree
    m = trimesh.load(os.path.join(ROOT, 'data', 'duct_v5_mm.stl'), force='mesh'); m.merge_vertices()
    trimesh.repair.fix_normals(m)
    A = np.load(os.path.join(ROOT, 'data', 'conducto_aux.npz'))
    V = m.vertices.copy(); x = V[:, 0]
    cy = np.interp(x, A['xs'], A['cy']); cz = np.interp(x, A['xs'], A['cz'])
    mov = (x > XA) & ~((x > XC0) & (x < XC1))              # vértices que pueden moverse
    # campo de distancias a las piezas en una rejilla de 1,5 mm (transformada de distancia euclídea): consultarlo es
    # ~100 veces más rápido que el árbol de puntos, y los optimizadores comprueban miles de diseños
    from scipy.ndimage import distance_transform_edt
    h = 1.5; lo = np.array([-790.0, 230.0, -125.0]); hi = np.array([-190.0, 490.0, 125.0])
    n = np.ceil((hi - lo) / h).astype(int) + 1
    occ = np.ones(n, dtype=bool)
    Q = A['obst']; ij = np.round((Q - lo) / h).astype(int); ok = np.all((ij >= 0) & (ij < n), axis=1)
    occ[tuple(ij[ok].T)] = False
    field = distance_transform_edt(occ, sampling=h).astype(np.float32)
    d = np.full(len(V), np.inf)
    _C.update(lo=lo, h=h, n=n, field=field)
    d[mov] = _field(V[mov])
    _C.update(mesh=m, V=V, cy=cy, cz=cz, mov=mov, d=d, tree=cKDTree(A['obst']), lab=A['lab'], names=[str(n) for n in A['names']], exit_y=(V[x > -210, 1].min(), V[x > -210, 1].max()),
              exit_z=(V[x > -210, 2].min(), V[x > -210, 2].max()))
    return _C


def _field(P):
    """Distancia (mm) de cada punto a la pieza más cercana, interpolada en el campo; fuera de la rejilla, 99."""
    from scipy.ndimage import map_coordinates
    c = _C; u = (np.asarray(P) - c['lo']) / c['h']
    return map_coordinates(c['field'], u.T, order=1, mode='constant', cval=99.0)


def _smooth(t):
    t = np.clip(t, 0, 1); return t * t * (3 - 2 * t)


def params(p):
    return {k: float(p.get(k, NEUTRO[k])) for k in NEUTRO}


def is_neutral(p):
    q = params(p); return all(abs(q[k] - NEUTRO[k]) < 1e-9 for k in NEUTRO)


def deformed_vertices(p):
    c = _load(); q = params(p); V = c['V']; x = V[:, 0]
    sy = np.ones(len(V)); sz = np.ones(len(V)); dy = np.zeros(len(V))
    b = (x > XA) & (x < XB1)                                  # tramo B: abombado (seno², nulo en los extremos)
    wb = np.sin(np.pi * (x[b] - XA) / (XB1 - XA)) ** 2
    sy[b] = sz[b] = 1 + (q['c_sB'] - 1) * wb
    dd = x >= XC1                                             # tramo D: difusor hasta la boca del plenum
    wd = _smooth((x[dd] - XC1) / (XD1 - XC1))
    sy[dd] = 1 + (q['c_sh'] - 1) * wd; sz[dd] = 1 + (q['c_sw'] - 1) * wd
    wy = np.sin(np.pi * np.clip((x[dd] - XC1) / (-205.0 - XC1), 0, 1)) ** 2   # subida del valle, nula en los extremos
    dy[dd] = q['c_dy'] * wy
    W = V.copy()
    W[:, 1] = c['cy'] + sy * (V[:, 1] - c['cy']) + dy
    W[:, 2] = c['cz'] + sz * (V[:, 2] - c['cz'])
    return W


def mesh(p):
    import trimesh
    c = _load()
    if is_neutral(p):
        return c['mesh'].copy()
    m = c['mesh'].copy(); m.vertices = deformed_vertices(p)
    return m


def check(p, floor=319.6, top=429.6, z_left=-79.2, z_right=80.8):
    """(ok, mensaje). Holgura con las piezas y que la boca del conducto quepa en la cara del plenum."""
    q = params(p)
    for k, lo, hi in VARS:
        if not (lo - 1e-6 <= q[k] <= hi + 1e-6):
            return False, f'{k} = {q[k]:g} fuera de rango [{lo:g}, {hi:g}]'
    if is_neutral(q):
        return True, 'conducto v5'
    c = _load()
    W = deformed_vertices(q); V = c['V']; mov = c['mov']
    # puntos que se mueven (más de 0,05 mm); dentro del plenum (x > -205) no hay pared de conducto
    moved = (np.linalg.norm(W[mov] - V[mov], axis=1) > 0.05) & (W[mov, 0] < -205.0)
    idx = np.where(moved)[0]
    if len(idx):
        dn = _field(W[mov][idx])
        need = np.minimum(HOLGURA, c['d'][mov][idx])
        slack = dn - need
        if (slack < -0.3).any():                              # 0,3 mm de tolerancia (rejilla de 1,5 mm)
            k = int(np.argmin(slack)); P = W[mov][idx][k]
            pieza = c['names'][int(c['lab'][c['tree'].query(P)[1]])]
            return False, (f'el conducto se acerca demasiado a una pieza ({pieza}): en x = {P[0]:.0f} mm faltan '
                           f'{-slack[k]:.1f} mm de holgura (mínimo {HOLGURA:g} mm, o lo que tenía el v5 si era menos); '
                           f'reduce c_sB, c_sw, c_sh o c_dy')
    xe = (W[:, 0] > -215) & (W[:, 0] < -205.5)                 # boca del conducto justo antes del plenum
    y0, y1 = W[xe, 1].min(), W[xe, 1].max(); z0, z1 = W[xe, 2].min(), W[xe, 2].max()
    if y0 < floor + 1.5 or y1 > top - 1.5:
        return False, f'la boca del conducto (y {y0:.1f}..{y1:.1f}) no cabe en la altura del plenum ({floor}..{top}); reduce c_sh'
    if z0 < z_left + 5 or z1 > z_right - 5:
        return False, (f'la boca del conducto (z {z0:.1f}..{z1:.1f}) no cabe en el ancho del plenum con 5 mm de margen '
                       f'(z_left {z_left:g}, z_right {z_right:g}); reduce c_sw o ensancha el plenum')
    return True, 'ok'


def areas(p, xs=(-760, -665, -575, -425, -370, -300, -210)):
    m = mesh(p); out = {}
    for x in xs:
        s = m.section(plane_origin=[x, 0, 0], plane_normal=[1, 0, 0])
        if s is None:
            continue
        pl, _ = s.to_2D(); out[x] = round(float(sum(abs(g.area) for g in pl.polygons_full)), 0)
    return out


if __name__ == '__main__':
    import json, sys, time
    t0 = time.time(); _load(); print('carga', round(time.time() - t0, 2), 's')
    for p in [NEUTRO, {'c_sB': 1.2}, {'c_sw': 1.5}, {'c_sh': 1.08}, {'c_dy': 15}, {'c_sw': 1.3, 'c_sh': 1.05, 'c_dy': 8, 'c_sB': 1.1}]:
        t0 = time.time(); ok, msg = check(p); print(json.dumps(p), ok, msg, f'{(time.time() - t0) * 1000:.0f} ms')
