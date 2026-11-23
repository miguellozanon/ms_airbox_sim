"""Lee un caso OpenFOAM (ascii) y calcula dp0 entrada->salida y el reparto conducto/plenum/runner."""
import os, re, sys, numpy as np

RHO = 1.153


def _body(path):
    s = open(path, 'rb').read().decode('latin1')
    return s


def read_list(s, start=0, dtype=float, vec=False):
    m = re.compile(r'(\d+)\s*\(', re.S).search(s, start)
    n = int(m.group(1)); i = m.end()
    j = s.index(')\n)', i) + 2 if vec else s.index('\n)', i)
    txt = s[i:j]
    if vec:
        a = np.array(txt.replace('(', ' ').replace(')', ' ').split(), dtype=dtype).reshape(n, 3)
    else:
        a = np.array(txt.split(), dtype=dtype)
    assert len(a) == n, (len(a), n)
    return a, j


def read_faces(path):
    s = _body(path)
    m = re.search(r'\n(\d+)\s*\n\(', s); n = int(m.group(1)); i = m.end()
    j = s.index('\n)', i)
    toks = re.findall(r'(\d+)\(([^)]*)\)', s[i:j])
    faces = [np.array(t[1].split(), dtype=np.int64) for t in toks]
    assert len(faces) == n
    return faces


class Mesh:
    def __init__(self, case):
        pm = os.path.join(case, 'constant/polyMesh')
        s = _body(f'{pm}/points'); self.points, _ = read_list(s[s.index('FoamFile'):], s.index('}') - s.index('FoamFile'), vec=True)
        s = _body(f'{pm}/owner'); self.owner, _ = read_list(s, s.index('}'), dtype=np.int64)
        s = _body(f'{pm}/neighbour'); self.neigh, _ = read_list(s, s.index('}'), dtype=np.int64)
        self.faces = read_faces(f'{pm}/faces')
        b = _body(f'{pm}/boundary')
        self.patches = {m.group(1): (int(m.group(3)), int(m.group(2))) for m in
                        re.finditer(r'\n\s*(\w+)\s*\n\s*\{[^}]*?nFaces\s+(\d+);\s*startFace\s+(\d+);', b)}
        X = self.points; nf = len(self.faces)
        fc = np.zeros((nf, 3)); A = np.zeros((nf, 3))
        nn = np.array([len(f) for f in self.faces])
        flat = np.concatenate(self.faces); off = np.r_[0, np.cumsum(nn)]
        idx = np.repeat(np.arange(nf), nn); np.add.at(fc, idx, X[flat]); fc /= nn[:, None]
        first = flat[off[:-1]]
        for k in range(1, nn.max() - 1):
            sel = np.where(nn > k + 1)[0]
            a = X[first[sel]]; bb = X[flat[off[sel] + k]]; c = X[flat[off[sel] + k + 1]]
            A[sel] += 0.5 * np.cross(bb - a, c - a)
        self.fc, self.Af = fc, A
        nc = max(self.owner.max(), self.neigh.max()) + 1
        V = np.zeros(nc); cc = np.zeros((nc, 3)); cnt = np.zeros(nc)
        d = np.einsum('ij,ij->i', fc, A) / 3; ni = len(self.neigh)
        np.add.at(V, self.owner, d); np.add.at(V, self.neigh, -d[:ni])
        np.add.at(cc, self.owner, fc); np.add.at(cnt, self.owner, 1); np.add.at(cc, self.neigh, fc[:ni]); np.add.at(cnt, self.neigh, 1)
        self.V = np.abs(V); self.cc = cc / cnt[:, None]; self.nc = nc


def read_field(case, t, name, vec=False):
    s = _body(os.path.join(case, str(t), name))
    i = s.index('internalField')
    if s[i:i + 60].split()[1].startswith('uniform'):
        return None
    a, _ = read_list(s, i, vec=vec)
    return a


def times(case):
    return sorted(int(d) for d in os.listdir(case) if d.isdigit() and int(d) > 0)


def evaluate(case, mesh=None, t=None, x_plenum=-0.220, y_runner=0.322):
    M = mesh or Mesh(case)
    t = t or times(case)[-1]
    p = read_field(case, t, 'p'); U = read_field(case, t, 'U', vec=True)
    pt = RHO * (p + 0.5 * (U ** 2).sum(1))           # Pa
    res = {}
    for name in ('inlet', 'outlet'):
        st, n = M.patches[name]
        own = M.owner[st:st + n]
        flux = np.abs(np.einsum('ij,ij->i', U[own], M.Af[st:st + n]))
        res[name] = (flux * pt[own]).sum() / flux.sum()
        res['Q_' + name] = flux.sum()
    cc = M.cc
    s = np.abs(cc[:, 0] - x_plenum) < 0.003
    w = U[s, 0] * M.V[s]; res['plenum_in'] = (w * pt[s]).sum() / w.sum()
    s = (np.abs(cc[:, 1] - y_runner) < 0.002) & (cc[:, 0] > -0.125) & (cc[:, 2] > 0)
    w = -U[s, 1] * M.V[s]; res['runner_in'] = (w * pt[s]).sum() / w.sum()
    res['dp0'] = res['inlet'] - res['outlet']
    res['dp_conducto'] = res['inlet'] - res['plenum_in']
    res['dp_plenum'] = res['plenum_in'] - res['runner_in']
    res['dp_runner'] = res['runner_in'] - res['outlet']
    res['Umax'] = float(np.sqrt((U ** 2).sum(1)).max())
    res['t'] = t
    return res, M


if __name__ == '__main__':
    case = sys.argv[1]
    M = None
    for t in times(case):
        r, M = evaluate(case, M, t)
        print(f"t={t:5d} dp0={r['dp0']:7.2f}  conducto={r['dp_conducto']:6.2f} plenum={r['dp_plenum']:6.2f} runner={r['dp_runner']:6.2f}  Umax={r['Umax']:5.1f}  Qout={r['Q_outlet']*RHO:.5f} kg/s")
