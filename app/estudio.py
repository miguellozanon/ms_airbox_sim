#!/usr/bin/env python3
"""Comparación de optimizadores sobre la cola de simulaciones (fase B del plan del paper).

Se activa con C:\\ms_airbox\\estudios\\<estudio>.json ("activo": true); hay dos: comparacion.json (los métodos en el
espacio base, comparación justa) y ampliado.json (búsqueda del mejor diseño en el espacio ampliado por el CAD). Lo ejecuta el trabajador (lanzador.py)
en un hilo propio, así que arranca, se para y se retoma con él. Cada ejecución:
  1. toma n_inicial casos ya calculados del muestreo (los mismos para todos los métodos con la misma semilla);
  2. pide al optimizador un diseño, lo encola como un caso más (prefijo 5000+, detrás del muestreo) y espera;
  3. repite hasta gastar el presupuesto de simulaciones nuevas.
Todo queda en estudios\\comparacion\\<id>.json (y <id>_llm.jsonl para los agentes LLM) y en resumen.csv.
"""
import csv, glob, json, os, threading, time, traceback
import numpy as np

import optim

QUEUE = os.environ.get('AIRBOX_QUEUE', '/mnt/c/ms_airbox/cola')
EST = os.path.join(os.path.dirname(QUEUE.rstrip('/')), 'estudios')
ESTUDIOS = ('comparacion', 'ampliado', 'conjunto')
BRIDGE = os.path.join(QUEUE, 'llm')
LOCK = threading.Lock()


def cfg_path(name):
    return os.path.join(EST, f'{name}.json')


def out_dir(name):
    return os.path.join(EST, name)


def now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def rjson(p, d=None):
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return d


def wjson(p, o):
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(o, f, indent=1, ensure_ascii=False)
    for _ in range(10):
        try:
            os.replace(tmp, p); return
        except OSError:
            time.sleep(0.2)


def log(msg):
    with open(os.path.join(QUEUE, 'lanzador.log'), 'a', encoding='utf-8') as f:
        f.write(f'{now()}  [estudio] {msg}\n')


def key(p, sp):
    p = sp.fill(p)
    return tuple(round(float(p[k]), 2) for k in sp.names)


def results_by_name():
    """Resultados de la cola: nombre -> resultado (hechos y fallidos)."""
    out = {}
    for d in ('hechos', 'fallidos'):
        for f in glob.glob(os.path.join(QUEUE, d, '*.json')):
            r = rjson(f, {}) or {}
            j = r.get('job', {})
            if j.get('name'):
                out[j['name']] = r
    return out


def summary_info(r):
    m = r.get('metrics') or {}
    inf = {k: m.get(k) for k in ('dp0_O', 'mezcla_ext', 'runner_O', 'plenum', 'conducto', 'backflow_O')}
    inf['vol_plenum_L'] = r.get('vol_plenum_L')
    return inf


class Run:
    def __init__(self, spec, cfg, study='comparacion'):
        self.study = study; self.cfg_path = cfg_path(study); out = out_dir(study)
        self.id = spec['id']; self.method = spec['metodo']; self.seed = int(spec['semilla'])
        self.sp = optim.space(cfg.get('espacio', 'base'))
        self.B = int(spec.get('presupuesto', cfg.get('presupuesto', 40)))
        self.n0 = spec.get('n_inicial', cfg.get('n_inicial', 8))
        self.n_prev = int(spec.get('mejores_previos', cfg.get('mejores_previos', 0)))
        self.prio = int(cfg.get('prioridad', 5000))
        self.obj = cfg.get('objetivo', 'dp0_ext'); self.h = cfg.get('h', 0.007); self.niter = cfg.get('niter', 1000)
        self.path = os.path.join(out, f'{self.id}.json')
        self.st = rjson(self.path) or {'id': self.id, 'metodo': self.method, 'semilla': self.seed, 'presupuesto': self.B,
                                       'objetivo': self.obj, 'espacio': self.sp.name, 'inicio': now(),
                                       'evaluaciones': [], 'pendiente': None}
        kw = {}
        if self.method.startswith('llm'):
            L = {**cfg.get('llm', {}), **spec.get('llm', {})}
            kw = dict(backend=L.get('backend', 'ollama'), model=L.get('modelo'), temperature=L.get('temperatura'),
                      num_ctx=int(L.get('num_ctx', 24576)), think=bool(L.get('think', True)),
                      bridge_dir=BRIDGE if L.get('puente', True) else None,
                      log_path=os.path.join(out, f'{self.id}_llm.jsonl'), run_id=self.id)
            self.st.setdefault('llm', {'backend': kw['backend'], 'modelo': kw['model'] or optim.DEFAULT_MODEL[kw['backend']],
                                       'temperatura': kw['temperature']})
        self.opt = optim.make(self.method, np.random.default_rng(self.seed), sp=self.sp, **kw)
        self.penalty = None

    def save(self):
        wjson(self.path, self.st)

    def initial(self, doe_names, res):
        rng = np.random.default_rng(1000 + self.seed)
        ok = [n for n in doe_names if res.get(n, {}).get('status') == 'done']
        if self.n0 == 'todos':
            pick = list(ok)
        else:
            pick = [ok[i] for i in sorted(rng.choice(len(ok), int(self.n0), replace=False))]
        if self.n_prev:                                  # además, los mejores casos ya calculados que caben en este espacio
            prev = sorted(((r['metrics'][self.obj], n) for n, r in res.items() if r.get('status') == 'done' and n not in pick
                           and not n.startswith(self.id + '_') and self.sp.contains(r.get('job', {}).get('params', {}))
                           and r.get('job', {}).get('h') == self.h and (r.get('metrics') or {}).get(self.obj) is not None))
            pick += [n for _, n in prev[:self.n_prev]]
        ys = [res[n]['metrics'][self.obj] for n in ok]
        self.penalty = 1.2 * max(ys)                    # diseños que fallan o incumplen el volumen real
        return pick

    def evaluated_new(self):
        return [e for e in self.st['evaluaciones'] if e['origen'] != 'inicial']

    def run(self, doe_names):
        res = results_by_name()
        if not self.st['evaluaciones']:
            for n in self.initial(doe_names, res):
                r = res[n]; q = self.sp.fill(r['job']['params']); p = {k: q[k] for k in self.sp.names}
                self.st['evaluaciones'].append({'origen': 'inicial', 'caso': n, 'params': p,
                                                'y': r['metrics'][self.obj], 'info': summary_info(r), 't': now()})
            self.save(); log(f'{self.id}: empieza con {len(self.st["evaluaciones"])} casos ya calculados (espacio {self.sp.name})')
        else:
            self.initial(doe_names, res)                # solo para la penalización
            log(f'{self.id}: se retoma con {len(self.evaluated_new())}/{self.B} simulaciones hechas')
        self.replay()
        while len(self.evaluated_new()) < self.B:
            if os.path.exists(os.path.join(QUEUE, 'STOP')) or not (rjson(self.cfg_path, {}) or {}).get('activo'):
                log(f'{self.id}: en pausa'); return False
            pend = self.st.get('pendiente')
            if not pend:
                left = self.B - len(self.evaluated_new())
                u = self.opt.ask(budget_left=left) if self.method.startswith('llm') else self.opt.ask()
                p = self.sp.to_params(u)
                k = len(self.evaluated_new()) + 1
                name = f'{self.id}_{k:02d}'
                usados = set(results_by_name()) | {os.path.basename(f).split('_', 1)[1][:-5] for d in ('pendientes', 'en_curso')
                                                    for f in glob.glob(os.path.join(QUEUE, d, '*.json'))}
                while name in usados:                    # tras rehacer una ejecución, el nombre puede existir ya en la cola
                    name += 'r'
                pend = {'caso': name, 'params': p, 'pedido': now(), 'ask': getattr(self.opt, 'last', {})}
                cache = self.lookup(p)
                if cache:
                    pend['cache'] = cache
                else:
                    job = {'name': name, 'params': self.sp.full(p), 'h': self.h, 'niter': self.niter,
                           'nota': f'estudio {self.study}/{self.id} ({self.method}, espacio {self.sp.name}), evaluación {k}/{self.B}'}
                    wjson(os.path.join(QUEUE, 'pendientes', f'{self.prio + 10 * int(self.seed)}_{name}.json'), job)
                self.st['pendiente'] = pend; self.save()
            r = None
            while r is None:
                res = results_by_name()
                r = res.get(pend.get('cache') or pend['caso'])
                if r is None:
                    if not (rjson(self.cfg_path, {}) or {}).get('activo'):
                        return False
                    time.sleep(20)
            if r.get('status') == 'done':
                y = r['metrics'][self.obj]; info = summary_info(r)
            else:
                y = self.penalty; info = {'estado': r.get('status'), 'nota': r.get('nota') or (r.get('error') or '')[-200:]}
            e = {'origen': 'cache' if pend.get('cache') else 'nuevo', 'caso': pend.get('cache') or pend['caso'],
                 'params': pend['params'], 'y': y, 'info': info, 'ask': pend.get('ask'), 't': now()}
            self.st['evaluaciones'].append(e); self.st['pendiente'] = None; self.save()
            self.opt.tell(self.sp.to_unit(pend['params']), y, info)
            best = min(x['y'] for x in self.st['evaluaciones'])
            log(f'{self.id}: {len(self.evaluated_new())}/{self.B}  {e["caso"]} = {y:.1f} Pa  (mejor {best:.1f})')
        self.st['fin'] = now(); self.save(); log(f'{self.id}: terminado')
        return True

    def replay(self):
        """Reconstruye el estado del optimizador al retomar. Los métodos clásicos son deterministas con su semilla:
        se les vuelve a pedir cada propuesta y se les da el resultado guardado, así CMA-ES (población, sigma) y el
        patrón (paso) quedan exactamente donde estaban. Si una propuesta no coincide con la guardada (p. ej. tras un
        cambio de código), se le da igualmente el punto guardado y se anota. A los agentes LLM no se les vuelve a
        preguntar (sería caro y no determinista): solo se les da el historial."""
        ini = [e for e in self.st['evaluaciones'] if e['origen'] == 'inicial']
        new = [e for e in self.st['evaluaciones'] if e['origen'] != 'inicial']
        for e in ini:
            self.opt.tell(self.sp.to_unit(e['params']), e['y'], e.get('info'))
        pend = self.st.get('pendiente')
        replay = not self.method.startswith('llm')
        mism = 0
        for k, e in enumerate(new + ([pend] if pend else [])):
            u_rec = self.sp.to_unit(e['params'])
            if replay:
                try:
                    u = self.opt.ask()
                    if not np.allclose(u, u_rec, atol=1e-6):
                        mism += 1
                        if mism == 1:
                            log(f'{self.id}: al reconstruir, la propuesta {k + 1} no coincide con la guardada; se sigue con la guardada')
                except Exception as ex:
                    replay = False; log(f'{self.id}: no se puede reconstruir preguntando ({ex}); solo historial')
            if e is not pend:
                self.opt.tell(u_rec, e['y'], e.get('info'))
        if replay:
            log(f'{self.id}: estado reconstruido ({len(new)} evaluaciones{", " + str(mism) + " sin coincidir" if mism else ", todas coinciden"})')

    def lookup(self, p):
        """Un diseño idéntico (a 0,01 mm) ya calculado: se reutiliza (cuenta como evaluación, sin coste)."""
        kp = key(p, self.sp)
        for n, r in results_by_name().items():
            if r.get('status') == 'done':
                q = r.get('job', {}).get('params', {})
                if self.sp.contains(q) and key(q, self.sp) == kp and r.get('job', {}).get('h') == self.h:
                    return n
        return None


def write_summary(study='comparacion'):
    OUT = out_dir(study); rows = []
    for f in sorted(glob.glob(os.path.join(OUT, '*.json'))):
        st = rjson(f, {}) or {}
        if 'evaluaciones' not in st:
            continue
        ev = st['evaluaciones']; ini = [e['y'] for e in ev if e['origen'] == 'inicial']
        best = min(ini) if ini else float('inf'); k = 0
        rows.append({'id': st['id'], 'metodo': st['metodo'], 'semilla': st['semilla'], 'evaluacion': 0, 'mejor': round(best, 2), 'y': ''})
        for e in ev:
            if e['origen'] == 'inicial':
                continue
            k += 1; best = min(best, e['y'])
            rows.append({'id': st['id'], 'metodo': st['metodo'], 'semilla': st['semilla'], 'evaluacion': k,
                         'mejor': round(best, 2), 'y': round(e['y'], 2), 'caso': e['caso']})
    if rows:
        with open(os.path.join(OUT, 'resumen.csv'), 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=['id', 'metodo', 'semilla', 'evaluacion', 'mejor', 'y', 'caso']); w.writeheader(); w.writerows(rows)


def doe_names(fname='doe150_sobol2026.json'):
    d = rjson(os.path.join(EST, fname), []) or []
    return [j['name'] for j in d]


def doe_complete(fname='doe150_sobol2026.json'):
    res = results_by_name(); names = doe_names(fname)
    return names and all(n in res for n in names)


_AVISADO = {}


def llm_ready(spec, cfg):
    """¿Está listo el backend del agente? Lo dice el panel en cola/llm/estado.json (Ollama con el modelo, clave de Anthropic)."""
    if not spec['metodo'].startswith('llm'):
        return True
    L = {**cfg.get('llm', {}), **spec.get('llm', {})}
    be = L.get('backend', 'ollama'); model = L.get('modelo') or optim.DEFAULT_MODEL[be]
    st = rjson(os.path.join(BRIDGE, 'estado.json'), {}) or {}
    if be == 'ollama':
        ok = bool(st.get('ollama')) and (model in st.get('modelos', []) or model + ':latest' in st.get('modelos', []))
        why = 'Ollama no está abierto en Windows' if not st.get('ollama') else f'falta el modelo {model} (ollama pull {model})'
    else:
        ok = bool(st.get('anthropic_clave')); why = 'no hay clave de Anthropic en C:\\ms_airbox\\secretos'
    if not ok and time.time() - _AVISADO.get(spec['id'], 0) > 6 * 3600:
        _AVISADO[spec['id']] = time.time(); log(f'{spec["id"]}: en espera, {why}')
    return ok


def run_all(study='comparacion'):
    """Bucle principal de un estudio: mantiene 'simultaneas' ejecuciones en marcha; se pausa si se desactiva."""
    OUT = out_dir(study); os.makedirs(OUT, exist_ok=True)
    running = {}
    while True:
        cfg = rjson(cfg_path(study), {}) or {}
        mfile = cfg.get('muestreo', 'doe150_sobol2026.json')
        for k in [k for k, t in running.items() if not t.is_alive()]:
            running.pop(k)
        if not cfg.get('activo') or (cfg.get('esperar_muestreo', True) and not doe_complete(mfile)):
            time.sleep(60); continue
        names = doe_names(mfile)
        todo = [s for s in cfg.get('ejecuciones', []) if s.get('activa', True) and s['id'] not in running
                and not (rjson(os.path.join(OUT, f'{s["id"]}.json'), {}) or {}).get('fin') and llm_ready(s, cfg)]
        n = int(cfg.get('simultaneas', 2))
        for spec in todo[:max(0, n - len(running))]:
            def go(spec=spec):
                try:
                    Run(spec, cfg, study).run(names)
                except Exception as e:
                    log(f'{spec["id"]}: ERROR {e}  {traceback.format_exc()[-400:]}')
                    time.sleep(600)
                finally:
                    write_summary(study)
            t = threading.Thread(target=go, daemon=True); t.start(); running[spec['id']] = t
            time.sleep(5)
        if not running and not todo:
            write_summary(study)
        time.sleep(30)


def start_all():
    for s in ESTUDIOS:
        threading.Thread(target=run_all, args=(s,), daemon=True, name=f'estudio_{s}').start()


if __name__ == '__main__':
    run_all()
