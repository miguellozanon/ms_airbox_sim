"""Geometría paramétrica plenum + runner (CadQuery) unida al conducto real de v5.
Unidades mm en CadQuery; el STL final se escribe en metros con regiones inlet/outlet/wall.
"""
import sys, json, math, numpy as np, trimesh
import cadquery as cq
import os
ROOT = os.environ.get('AIRBOX_ROOT', os.path.dirname(os.path.abspath(__file__)))

# --- datos medidos en v5 (mm) ---
O = np.array([-98.37, 255.58, 31.62])                 # centro de la salida (cara de la mariposa)
D_OUT = -np.array([0.900, 0.387, 0.198]); D_OUT /= np.linalg.norm(D_OUT)   # direccion del flujo a la salida
FLOOR, TOP = 319.6, 429.6
X_DUCT_END = -205.0                                    # escalon de entrada al plenum


def duct_mesh(cut=-203.0, p=None):
    """Conducto de entrada: el v5 tal cual o, con parámetros c_* (conducto.py), deformado con la boca oval y el paso
    por el chasis intactos."""
    if p and any(k.startswith('c_') for k in p):
        import conducto
        m = conducto.mesh(p)
    else:
        m = trimesh.load(os.path.join(ROOT, 'data', 'duct_v5_mm.stl'), force='mesh'); m.merge_vertices()
    trimesh.repair.fix_normals(m)
    d = m.slice_plane([cut, 0, 0], [-1, 0, 0], cap=True)
    return d


def runner_path(R, L2, Lext, D, theta=0.0):
    """Eje del runner: tramo recto inclinado theta (grados, desde la vertical hacia la mariposa)
    que nace en el suelo del plenum -> arco de radio R -> salida O -> prolongacion Lext.
    Devuelve (puntos, E = punto del eje en el suelo, longitud del tramo recto)."""
    h = np.array([D_OUT[0], 0, D_OUT[2]]); h /= np.linalg.norm(h); yv = np.array([0, 1.0, 0])
    alpha = math.atan2(-D_OUT[1], np.linalg.norm([D_OUT[0], D_OUT[2]]))
    beta = math.pi / 2 - alpha
    th = math.radians(theta)
    if th >= beta - 1e-3:
        raise ValueError('theta demasiado grande')
    P_end = O - L2 * D_OUT
    S_bot = P_end - R * (math.cos(th) - math.cos(beta)) * h - R * (math.sin(th) - math.sin(beta)) * yv
    if S_bot[1] > FLOOR - 1:
        raise ValueError(f'R demasiado grande: inicio del arco en y={S_bot[1]:.1f}')
    back = -math.sin(th) * h + math.cos(th) * yv              # hacia el plenum
    Ls = (FLOOR - S_bot[1]) / math.cos(th)
    E = S_bot + Ls * back
    pts = [S_bot + (Ls + 15 / math.cos(th)) * back, S_bot]
    C = S_bot + R * (math.cos(th) * h + math.sin(th) * yv)
    for ph in np.linspace(th, beta, 24)[1:]:
        pts.append(C - R * math.cos(ph) * h - R * math.sin(ph) * yv)
    pts.append(O)
    pts.append(O + Lext * D_OUT)
    return np.array(pts), E, Ls


def step_to_mesh(step, h):
    import gmsh
    gmsh.initialize(); gmsh.option.setNumber('General.Terminal', 0)
    gmsh.model.add('m'); gmsh.model.occ.importShapes(step); gmsh.model.occ.synchronize()
    gmsh.option.setNumber('Mesh.MeshSizeMax', h); gmsh.option.setNumber('Mesh.MeshSizeMin', h / 4)
    gmsh.option.setNumber('Mesh.MeshSizeFromCurvature', 24)
    gmsh.model.mesh.generate(2)
    nt, xyz, _ = gmsh.model.mesh.getNodes(); X = xyz.reshape(-1, 3); mp = {k: i for i, k in enumerate(nt)}
    T = []; vacias = []
    for d, t in gmsh.model.getEntities(2):
        et, tags, nodes = gmsh.model.mesh.getElements(d, t)
        if not len(nodes):
            vacias.append(t); continue
        T.append(np.array([mp[k] for k in nodes[0]]).reshape(-1, 3))
    if vacias:
        gmsh.finalize()
        raise ValueError(f'gmsh no malla {len(vacias)} caras (superficies degeneradas)')
    gmsh.finalize()
    m = trimesh.Trimesh(X, np.vstack(T), process=True); m.merge_vertices()
    return m


def build(p, out_stl, info=None):
    D = p.get('D', 42.0); R = p.get('R', 38.0); L2 = p.get('L2', 0.0); Lext = p.get('Lext', 2 * D)
    rb = p.get('rb', 0.0)                       # radio del bellmouth
    x0 = X_DUCT_END; x1 = p.get('x_rear', -53.3)
    zl, zr = p.get('z_left', -79.2), p.get('z_right', 80.8)
    r_plan = p.get('r_plan', 0.0)               # redondeo vertical esquina trasera izquierda (como v5)
    r_plan_front = p.get('r_plan_front', 0.0)
    r_edge = p.get('r_edge', 3.0)
    theta = p.get('theta', 0.0)
    pts, S_bot, Lv = runner_path(R, L2, Lext, D, theta)
    # plenum
    Lt = p.get('Lt', 0.0)                       # longitud de la transicion conducto->plenum (loft)
    if 0 < Lt < 3.0:                            # < r_edge: sin sentido fisico y deja superficies degeneradas
        Lt = 0.0
    xb = x0 + Lt if Lt > 0 else x0 - 2.0
    box = cq.Workplane('XY').box(x1 - xb, TOP - FLOOR, zr - zl, centered=False).translate((xb, FLOOR, zl))
    if r_plan > 0:
        box = box.edges('|Y').edges('>X').edges('<Z').fillet(r_plan)
    if r_plan_front > 0:
        box = box.edges('|Y').edges('<X').fillet(r_plan_front)
    if Lt > 0:
        # seccion real del conducto en x0 (muestreada) -> rectangulo del plenum en x0+Lt
        dm = duct_mesh(cut=x0, p=p)
        sec = dm.section(plane_origin=np.array([x0 - 1.5, 0, 0.]), plane_normal=np.array([1., 0, 0]))
        d = max(sec.discrete, key=len)[:, 1:]
        c = d.mean(0); ang = np.arctan2(d[:, 1] - c[1], d[:, 0] - c[0])
        P = []
        for a in np.linspace(-np.pi, np.pi, 33)[:-1]:
            i = np.argmin(np.abs(np.angle(np.exp(1j * (ang - a))))); P.append(d[i])
        P = np.array(P)
        # rectangulo destino con el mismo numero de puntos (por angulo, sobre el contorno)
        hy, hz = (TOP - FLOOR) / 2, (zr - zl) / 2; cy, cz = 0.5 * (FLOOR + TOP), 0.5 * (zl + zr)
        Q = []
        for a in np.linspace(-np.pi, np.pi, 33)[:-1]:
            ux, uz = np.cos(a), np.sin(a); t = min(hy / max(abs(ux), 1e-9), hz / max(abs(uz), 1e-9))
            Q.append((cy + t * ux, cz + t * uz))
        w0 = cq.Wire.makePolygon([cq.Vector(x0 - 2.0, y, z) for y, z in P], close=True)
        w1 = cq.Wire.makePolygon([cq.Vector(x0 + Lt + 0.5, y, z) for y, z in Q], close=True)
        loft = cq.Solid.makeLoft([w0, w1], ruled=True)
        box = box.union(cq.Workplane('XY').add(loft))
    # runner: barrido de un circulo a lo largo del eje, con camino EXACTO: arco de radio R + recta (L2 + extension).
    # (Antes era una spline por puntos: con L2 > 0 oscilaba al pasar de curva a recta y dejaba rendijas.)
    top = cq.Vector(*pts[0]); s0 = cq.Vector(*pts[1])
    ax0 = pts[0] - pts[1]; Lc = float(np.linalg.norm(ax0)); ax0 = ax0 / Lc
    vert = cq.Workplane('XY').add(cq.Solid.makeCylinder(D / 2, Lc, s0, cq.Vector(*ax0)))
    P_end = pts[-3]; mid = pts[1 + (len(pts) - 4) // 2]; tip = pts[-1]
    edges = [cq.Edge.makeThreePointArc(cq.Vector(*pts[1]), cq.Vector(*mid), cq.Vector(*P_end)),
             cq.Edge.makeLine(cq.Vector(*P_end), cq.Vector(*tip))]
    path_wire = cq.Workplane('XY').add(cq.Wire.assembleEdges(edges))
    xd = np.cross(ax0, [0, 0, 1.0]); xd = xd / np.linalg.norm(xd)
    sweep = cq.Workplane(cq.Plane(origin=tuple(pts[1]), xDir=tuple(xd), normal=tuple(-ax0))).circle(D / 2).sweep(path_wire, transition='right')
    solid = box.union(vert).union(sweep)
    if rb > 0:
        # bellmouth: perfil de cuarto de circulo revolucionado, bajo el suelo, recortado por la pared trasera
        a = cq.Vector(S_bot[0], FLOOR, S_bot[2])
        prof = (cq.Workplane('XY').moveTo(0, 0).lineTo(D / 2 + rb, 0)
                .radiusArc((D / 2, -rb), -rb).lineTo(0, -rb).close())
        bell = prof.revolve(360, (0, 0, 0), (0, 1, 0))
        ang = math.degrees(math.acos(max(-1, min(1, float(ax0 @ np.array([0, 1.0, 0]))))))
        if ang > 0.01:
            rax = np.cross([0, 1.0, 0], ax0); rax = rax / np.linalg.norm(rax)
            bell = bell.rotate((0, 0, 0), tuple(rax), ang)
        lift = -(D / 2 + rb) * math.tan(math.radians(theta))
        Eb = np.array([S_bot[0], FLOOR, S_bot[2]]) + lift * ax0
        bell = bell.translate(tuple(Eb))
        clip = cq.Workplane('XY').box(400, 200, 400).translate((x1 - 200, FLOOR - 50, 0))
        solid = solid.union(bell.intersect(clip))
    try:
        solid = solid.edges(cq.selectors.BoxSelector((x0 - 1, FLOOR + 1, zl - 1), (x1 + 1, TOP + 1, zr + 1))).fillet(r_edge) if r_edge > 0 else solid
    except Exception:
        pass
    vol_plenum = box.val().Volume() / 1e6
    step = out_stl.replace('.stl', '_plenum_runner.step')
    cq.exporters.export(solid, step)
    pr = step_to_mesh(step, p.get('hmesh', 1.5)); trimesh.repair.fix_normals(pr)
    duct = duct_mesh(cut=x0 - 1.0, p=p); duct.merge_vertices(); trimesh.repair.fix_normals(duct)
    print('watertight pr',pr.is_watertight,'duct',duct.is_watertight, 'vol pr', pr.volume/1e6, 'duct', duct.volume/1e6)
    import manifold3d
    full = trimesh.boolean.union([duct, pr], engine='manifold')
    full.merge_vertices(); trimesh.repair.fix_normals(full)
    # regiones
    n = full.face_normals; c = full.triangles_center
    inlet = (c[:, 0] < -832.0) & (np.abs(n[:, 0]) > 0.99)
    pout = pts[-1]
    outlet = (np.abs((c - pout) @ D_OUT) < 0.3) & (np.abs(n @ D_OUT) > 0.99)
    wall = ~(inlet | outlet)
    V = full.vertices / 1000
    with open(out_stl, 'w') as f:
        for name, sel in [('inlet', inlet), ('outlet', outlet), ('wall', wall)]:
            f.write(f'solid {name}\n')
            for t, nn in zip(full.faces[sel], n[sel]):
                f.write(f' facet normal {nn[0]:.6e} {nn[1]:.6e} {nn[2]:.6e}\n  outer loop\n')
                for q in V[t]: f.write(f'   vertex {q[0]:.7e} {q[1]:.7e} {q[2]:.7e}\n')
                f.write('  endloop\n endfacet\n')
            f.write(f'endsolid {name}\n')
    res = dict(p, Lt_efectivo=Lt, vol_total_L=full.volume / 1e6, vol_plenum_box_L=vol_plenum, watertight=bool(full.is_watertight),
               A_in_cm2=full.area_faces[inlet].sum() / 100, A_out_cm2=full.area_faces[outlet].sum() / 100,
               entry_xz=[round(S_bot[0], 1), round(S_bot[2], 1)], Lv=round(Lv, 1))
    if info: json.dump(res, open(info, 'w'), indent=1)
    return res


def _attempt(p, out, info, q):
    import traceback
    try:
        q.put(('ok', build(p, out, info)))
    except Exception:
        q.put(('err', traceback.format_exc()))


def build_robust(p, out, info=None, t_attempt=110):
    """Algunas combinaciones dejan en el CAD una cara que gmsh no malla o en la que se cuelga (p. ej. el labio cortado
    por la pared trasera casi en tangencia: rb = 11,21 falla, 11,16 se cuelga, 11,11 y 11,26 van bien). Si ocurre, se
    repite con el labio (o la curva) movidos unas centésimas de milímetro -muy por debajo de la resolución de la malla
    CFD- y se anota la perturbación. Cada intento va en un proceso propio con su límite de tiempo."""
    import multiprocessing as mp
    k = 'rb' if p.get('rb', 0) > 0.2 else 'R'
    deltas = [0.0, 0.05, -0.1, 0.1, -0.2]
    last = None
    for d in deltas:
        q = dict(p); q[k] = round(p.get(k, 0) + d, 2)
        ctx = mp.get_context('fork'); qu = ctx.Queue()
        pr = ctx.Process(target=_attempt, args=(q, out, None, qu)); pr.start(); pr.join(t_attempt)
        if pr.is_alive():
            pr.kill(); pr.join(); last = f'{k} = {q[k]}: colgado más de {t_attempt} s'
        else:
            try:
                st, res = qu.get(timeout=5)
            except Exception:
                st, res = 'err', 'sin resultado'
            if st == 'ok':
                if d:
                    res = dict(res, perturbacion={k: d}, **{k + '_construido': q[k]}); res[k] = p[k]
                    print(f'construido con {k} = {q[k]} (pedido {p[k]}): {last}', flush=True)
                if info:
                    json.dump(res, open(info, 'w'), indent=1, default=float)
                return res
            last = f'{k} = {q[k]}: ' + res.strip().splitlines()[-1]
        print('intento fallido:', last, flush=True)
    raise RuntimeError(f'geometría no construible tras {len(deltas)} intentos; último: {last}')


if __name__ == '__main__':
    p = json.loads(sys.argv[1]); print(build_robust(p, sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None))
