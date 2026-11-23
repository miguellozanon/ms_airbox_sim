#!/usr/bin/env python3
"""Muestreo inicial (DoE) del airbox: 7 variables de diseño, restricciones geométricas y de volumen.

Variables (mm):
  rb      radio del labio (bellmouth) de entrada del runner      [0, 14]
  R       radio de la curva del runner                           [20, 60]
  L2      tramo recto del runner antes de la mariposa             [0, 20]
  x_rear  posición de la pared trasera del plenum                [-56, -40]   (-40 = límite +13 mm)
  z_left  pared izquierda del plenum (lado opuesto al runner)    [-79.2, -64]
Los rangos son la caja que envuelve la región factible: fuera de ella el runner no cabe delante de la
pared trasera (la mariposa está fija) o el plenum baja de 2,6 L.
  Lt      longitud de la transición conducto -> plenum          [0, 40]   (< 3 mm se construye como 0)
  r_plan  redondeo vertical de la esquina trasera izquierda      [0, 100]
Fijos: D = 44 (diámetro de la mariposa Keihin), z_right = 80.8, r_edge = 3, theta = 0, Lext = 2D.
No se varía la altura del plenum: la boca del conducto ocupa y = 324-425 mm dentro de 319,6-429,6.

Restricciones (comprobación rápida, sin CAD):
  - la curva cabe (geom.runner_path no da error);
  - el tubo del runner queda dentro del plenum: borde trasero de su entrada <= x_rear - 2 mm,
    y el labio cabe a lo ancho: z_entrada ± (D/2 + rb) dentro de [z_left + 2, z_right - 2];
  - redondeo de planta <= 0,9 · min(largo, ancho) del plenum;
  - volumen estimado del plenum >= 2,6 L (el trabajador comprueba después el volumen real >= 2,5 L).

Muestreo: secuencia de Sobol aleatorizada (semilla fija, reproducible) filtrada por las restricciones;
se toman los primeros N puntos factibles, que conservan la baja discrepancia dentro de la región factible.
Uso: python doe.py N [semilla] [base|ampliado] > lote.json
"""
import json, math, sys
import numpy as np

VARS = [('rb', 0.0, 14.0), ('R', 20.0, 60.0), ('L2', 0.0, 20.0), ('x_rear', -56.0, -40.0),
        ('z_left', -79.2, -64.0), ('Lt', 0.0, 40.0), ('r_plan', 0.0, 100.0)]
FIXED = {'D': 44.0, 'z_right': 80.8, 'r_edge': 3.0, 'theta': 0.0}

# Espacio ampliado (envolvente del CAD de la MS9, 20/11/2026; coordenadas propias = conjunto - t, t = [1038.24, 442.15, 615.76]):
#  - el Airbox4 actual ocupa por fuera x <= -34,3 y z en [-87,5, 92,5]; con pared de 3 mm, dentro de esa envolvente caben
#    z_left >= -84 y z_right <= 89 sin tocar nada;
#  - detrás no hay piezas rígidas a menos de 34 mm (subchasis) hasta x = 0, pero ahí irá el depósito (en diseño):
#    se permite un margen blando de 22 mm más allá del Airbox4 (x_rear <= -15, cara exterior en -12), ~0,4 L al depósito;
#  - lateral: la tapa (Cubre) está a z = ±106, 14 mm por fuera de z_right = 89 con pared; el techo no se toca (depósito encima);
#  - R por encima de ~68 no cabe (la curva bajaría por debajo del suelo), rb hasta 20 (antes 14).
VARS_AMP = [('rb', 0.0, 20.0), ('R', 20.0, 68.0), ('L2', 0.0, 20.0), ('x_rear', -56.0, -15.0),
            ('z_left', -84.0, -64.0), ('z_right', 80.8, 89.0), ('Lt', 0.0, 40.0), ('r_plan', 0.0, 100.0)]
FIXED_AMP = {'D': 44.0, 'r_edge': 3.0, 'theta': 0.0}
# Espacio conjunto (21/11/2026): el ampliado + el conducto de entrada (conducto.py: boca oval del v5 y paso por el chasis
# fijos; abombado del tramo toma-chasis, subida del valle tras el chasis y difusor de salida en ancho y alto).
import conducto
VARS_CONJ = VARS_AMP + conducto.VARS
SPACES = {'base': (VARS, FIXED), 'ampliado': (VARS_AMP, FIXED_AMP), 'conjunto': (VARS_CONJ, FIXED_AMP)}
X0 = -205.0                         # entrada del plenum (escalón del conducto)
VMIN_EST = 2.6                      # L


def check(p):
    import geom
    D = FIXED['D']
    try:
        pts, E, Ls = geom.runner_path(p['R'], p['L2'], 2 * D, D, 0.0)
    except ValueError:
        return False, (f'la curva no cabe: con R = {p["R"]:g} y L2 = {p["L2"]:g} el arco empezaría por encima del suelo '
                       f'del plenum; reduce R o L2')
    if E[0] + D / 2 > p['x_rear'] - 2:
        return False, (f'el runner no cabe delante de la pared trasera: con R = {p["R"]:g} y L2 = {p["L2"]:g} su borde trasero '
                       f'queda en x = {E[0] + D / 2:.1f} mm y la pared debe estar al menos 2 mm detrás, es decir x_rear >= '
                       f'{E[0] + D / 2 + 2:.1f} (x crece hacia atrás: hay que AUMENTAR x_rear, o reducir R)')
    zr = p.get('z_right', FIXED['z_right']); zl = p['z_left']
    if E[2] + D / 2 + p['rb'] > zr - 2 or E[2] - D / 2 - p['rb'] < zl + 2:
        return False, (f'el labio no cabe a lo ancho: con rb = {p["rb"]:g} ocupa z de {E[2] - D / 2 - p["rb"]:.1f} a '
                       f'{E[2] + D / 2 + p["rb"]:.1f} mm y debe quedar a 2 mm de las paredes (z_left = {zl:g}, z_right = {zr:g}); '
                       f'reduce rb' + (' o aumenta z_right' if 'z_right' in p else ''))
    xb = X0 + p['Lt'] if p['Lt'] > 0 else X0 - 2
    Lx, W, H = p['x_rear'] - xb, zr - zl, geom.TOP - geom.FLOOR
    if p['r_plan'] > 0.9 * min(Lx, W):
        return False, 'redondeo de planta demasiado grande'
    vol = (Lx * W * H - (1 - math.pi / 4) * p['r_plan'] ** 2 * H) / 1e6
    if p['Lt'] > 0:
        vol += 0.5 * p['Lt'] * (W * H + 102 * 100) / 1e6        # loft aproximado (sección del conducto ~102x100)
    if vol < VMIN_EST:
        return False, f'volumen estimado {vol:.2f} L'
    if any(k.startswith('c_') for k in p):
        ok, info = conducto.check(p, geom.FLOOR, geom.TOP, zl, zr)
        if not ok:
            return False, info
    return True, round(vol, 2)


def design(n, seed=2026, space='base'):
    from scipy.stats import qmc
    VARS = SPACES[space][0]
    s = qmc.Sobol(d=len(VARS), scramble=True, seed=seed)
    lo = np.array([v[1] for v in VARS]); hi = np.array([v[2] for v in VARS])
    out, tried, why = [], 0, {}
    while len(out) < n and tried < 2 ** 16:
        X = qmc.scale(s.random(256), lo, hi); tried += 256
        for x in X:
            p = {k: round(float(v), 2) for (k, _, _), v in zip(VARS, x)}
            ok, info = check(p)
            if ok:
                p['vol_est_L'] = info; out.append(p)
                if len(out) == n:
                    break
            else:
                kk = info.split(' ')[0] if info.startswith('volumen') else info.split(':')[0]
                why[kk] = why.get(kk, 0) + 1
    return out, tried, why


def jobs(points, prefix='doe', h=0.007, niter=1000, space='base'):
    VARS, FIX = SPACES[space]
    js = []
    for i, p in enumerate(points, 1):
        params = {k: p[k] for k, _, _ in VARS}; params.update(FIX)
        js.append({'name': f'{prefix}{i:03d}', 'params': params, 'h': h, 'niter': niter,
                   'nota': f'DoE Sobol {len(VARS)}D ({space}), punto {i}; volumen estimado {p["vol_est_L"]} L'})
    return js


if __name__ == '__main__':
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 2026
    space = sys.argv[3] if len(sys.argv) > 3 else 'base'
    pts, tried, why = design(n, seed, space)
    print(f'{len(pts)} puntos factibles de {tried} probados; rechazos: {why}', file=sys.stderr)
    print(json.dumps(jobs(pts, {'base': 'doe', 'ampliado': 'amp'}.get(space, 'conj'), space=space), indent=1))
