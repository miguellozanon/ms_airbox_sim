import sys, json, numpy as np
import os
ROOT = os.environ.get('AIRBOX_ROOT', os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); import ofpost, geom
RHO = 1.153
def plane_pt(M, U, pt, q, tg, D):
    cc = M.cc * 1000; d = cc - q; al = d @ tg; rad = np.linalg.norm(d - np.outer(al, tg), axis=1)
    sel = (np.abs(al) < 1.0) & (rad < D / 2 + 1); w = (U[sel] @ tg) * M.V[sel]
    return (w * pt[sel]).sum() / w.sum(), float((w < 0).sum() / len(w))
def case_metrics(case, R, D, t0):
    M = None; out = []
    pts, S, Lv = geom.runner_path(R, 0, 2 * D, D)
    tgO = pts[-1] - pts[-2]; tgO /= np.linalg.norm(tgO)
    for t in ofpost.times(os.path.join(ROOT, 'cases', case)):
        if t < t0: continue
        r, M = ofpost.evaluate(os.path.join(ROOT, 'cases', case), M, t)
        p = ofpost.read_field(os.path.join(ROOT, 'cases', case), t, 'p') * RHO; U = ofpost.read_field(os.path.join(ROOT, 'cases', case), t, 'U', vec=True)
        pt = p + 0.5 * RHO * (U ** 2).sum(1)
        ptO, bf = plane_pt(M, U, pt, geom.O + 2 * tgO, tgO, D)
        out.append([r['dp0'], r['inlet'] - ptO, r['dp_conducto'], r['dp_plenum'], r['runner_in'] - ptO, bf])
    if not out:
        raise RuntimeError('no hay resultados guardados en la segunda mitad del cálculo (¿menos de 200 iteraciones?)')
    a = np.array(out)
    return dict(caso=case, dp0_O=round(a[:, 1].mean(), 1), dp0_O_std=round(a[:, 1].std(), 1), dp0_ext=round(a[:, 0].mean(), 1),
                conducto=round(a[:, 2].mean(), 1), plenum=round(a[:, 3].mean(), 1), runner_O=round(a[:, 4].mean(), 1),
                mezcla_ext=round((a[:, 0] - a[:, 1]).mean(), 1), backflow_O=round(a[:, 5].mean(), 3), n=len(a))
if __name__ == '__main__':
    res = case_metrics(sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4]))
    print(res); json.dump(res, open(f'metrics_{sys.argv[1]}.json', 'w'))
