#!/usr/bin/env python3
"""Optimizadores para el airbox: búsqueda por patrón, bayesiana (GP-EI), CMA-ES y agente LLM.

Todos trabajan en un espacio normalizado [0, 1]^d (Space: 'base' 7D o 'ampliado' 8D, doe.SPACES), con la misma interfaz «ask/tell» y la
misma regla de restricciones (doe.check): una propuesta que no cabe geométricamente se rechaza sin
gastar simulación. El objetivo es la pérdida de presión total con mezcla (dp0_ext, Pa), a minimizar.

La comparación (estudio.py) da a todos el mismo punto de partida (los mismos casos ya calculados del
muestreo) y el mismo presupuesto de simulaciones nuevas.
"""
import json, math, re, time, urllib.request, os
import numpy as np

import doe

class Space:
    """Espacio de diseño normalizado [0, 1]^d de doe.SPACES[nombre] ('base': 7 variables; 'ampliado': 8,
    envolvente del CAD). Cada optimizador lleva el suyo, así que pueden convivir estudios en espacios distintos."""

    def __init__(self, name='base'):
        self.name = name; self.vars, self.fixed = doe.SPACES[name]
        self.names = [v[0] for v in self.vars]; self.dim = len(self.names)
        self.lo = np.array([v[1] for v in self.vars]); self.hi = np.array([v[2] for v in self.vars])
        self._pool = {}

    def to_params(self, u):
        x = self.lo + np.clip(np.asarray(u, float), 0, 1) * (self.hi - self.lo)
        return {k: round(float(v), 2) for k, v in zip(self.names, x)}

    def fill(self, p):
        """Valores por defecto de las variables que un caso antiguo no tiene (conducto v5, pared derecha 80,8)."""
        return {**DEFAULTS, **p}

    def to_unit(self, p):
        p = self.fill(p)
        return (np.array([p[k] for k in self.names], float) - self.lo) / (self.hi - self.lo)

    def snap(self, u):
        """Redondea al paso de 0,01 mm con el que se simula (así la propuesta y su resultado coinciden)."""
        return self.to_unit(self.to_params(u))

    def full(self, p):
        """Parámetros completos para geom/doe.check: variables + fijos del espacio + fijos base (z_right...)."""
        return {**doe.FIXED, **self.fixed, **p}

    def contains(self, p):
        p = self.fill(p)
        return all(k in p and lo - 1e-6 <= float(p[k]) <= hi + 1e-6 for k, lo, hi in self.vars) and \
            all(abs(float(p.get(k, v)) - v) < 1e-6 for k, v in {**doe.FIXED, **self.fixed, **DEFAULTS}.items() if k not in self.names)

    def why_not(self, p):
        ok, info = doe.check(self.full(p))
        return None if ok else info

    def feasible(self, p):
        return self.why_not(p) is None

    def random_feasible(self, rng, n, tries=200000):
        out = []
        for _ in range(tries):
            u = rng.random(self.dim)
            if self.feasible(self.to_params(u)):
                out.append(u)
                if len(out) == n:
                    break
        return np.array(out)

    def feasible_pool(self, n=20000, seed=12345):
        """Bolsa fija de puntos factibles (se calcula una vez por espacio)."""
        if n not in self._pool:
            self._pool[n] = self.random_feasible(np.random.default_rng(seed), n, tries=2_000_000)
        return self._pool[n]


DEFAULTS = {'z_right': 80.8, **doe.conducto.NEUTRO}
_SPACES = {}


def space(name='base'):
    if name not in _SPACES:
        _SPACES[name] = Space(name)
    return _SPACES[name]


BASE = space('base')
NAMES, LO, HI, DIM = BASE.names, BASE.lo, BASE.hi, BASE.dim
to_params, to_unit, snap, feasible, why_not = BASE.to_params, BASE.to_unit, BASE.snap, BASE.feasible, BASE.why_not
random_feasible, feasible_pool = BASE.random_feasible, BASE.feasible_pool


# ------------------------------------------------------------------ base
class Optimizer:
    name = 'base'

    def __init__(self, rng, sp=None):
        self.rng = rng; self.sp = sp or BASE; self.X = []; self.y = []; self.info = []

    def tell(self, u, y, info=None):
        self.X.append(np.asarray(u, float)); self.y.append(float(y)); self.info.append(info or {})

    def best(self):
        i = int(np.argmin(self.y)); return self.X[i], self.y[i]

    def ask(self):
        raise NotImplementedError

    def state(self):
        return {}

    def load_state(self, st):
        pass


# ------------------------------------------------------------------ búsqueda por patrón (compass)
class Pattern(Optimizer):
    """Búsqueda por patrón (coordenada a coordenada, ±paso). Acepta una mejora si baja más de 'tol' (Pa);
    tras un barrido completo sin mejora, reduce el paso a la mitad."""
    name = 'patron'

    def __init__(self, rng, step=0.25, min_step=1 / 64, tol=1.5, sp=None):
        super().__init__(rng, sp); self.step = step; self.min_step = min_step; self.tol = tol
        self.center = None; self.fc = None; self.k = 0; self.pending = None

    def tell(self, u, y, info=None):
        super().tell(u, y, info)
        if self.center is None or (self.pending is None and y < self.fc):
            self.center, self.fc = np.asarray(u, float), y          # puntos iniciales: toma el mejor
            return
        if self.pending is not None and np.allclose(u, self.pending, atol=1e-6):
            self.pending = None
            if y < self.fc - self.tol:
                self.center, self.fc = np.asarray(u, float), y; self.k = 0
            else:
                self.k += 1

    def _known(self, u):
        for x, y in zip(self.X, self.y):
            if np.allclose(x, u, atol=1e-6):
                return y
        return None

    def ask(self):
        sp = self.sp
        for _ in range(4 * sp.dim * 40):
            if self.k >= 2 * sp.dim:
                self.k = 0; self.step /= 2
            if self.step < self.min_step:
                self.step = 0.25                                        # reinicio (no debería ocurrir en 40 evaluaciones)
            i, sgn = divmod(self.k, 2)
            u = self.center.copy(); u[i] = np.clip(u[i] + (1 if sgn == 0 else -1) * self.step, 0, 1); u = sp.snap(u)
            if np.allclose(u, self.center, atol=1e-6) or not sp.feasible(sp.to_params(u)):
                self.k += 1; continue
            y = self._known(u)
            if y is not None:                                           # ya calculado: se decide sin simular
                if y < self.fc - self.tol:
                    self.center, self.fc = u, y; self.k = 0
                else:
                    self.k += 1
                continue
            self.pending = u
            return u
        return sp.snap(sp.random_feasible(self.rng, 1)[0])

    def state(self):
        return {'step': self.step, 'k': self.k}

    def load_state(self, st):
        self.step = st.get('step', self.step); self.k = st.get('k', 0)


# ------------------------------------------------------------------ bayesiana (GP Matérn 5/2 ARD + EI)
class GP:
    def __init__(self, X, y):
        self.X = np.asarray(X); y = np.asarray(y, float); self.d = self.X.shape[1]
        self.mu, self.sd = y.mean(), y.std() + 1e-9; self.yn = (y - self.mu) / self.sd
        self._fit()

    @staticmethod
    def _k(A, B, ls, s2):
        d = np.sqrt(((A[:, None, :] - B[None, :, :]) ** 2 / ls ** 2).sum(-1)) * math.sqrt(5)
        return s2 * (1 + d + d ** 2 / 3) * np.exp(-d)

    def _nll(self, th):
        ls, s2, n2 = np.exp(th[:self.d]), np.exp(th[self.d]), np.exp(th[self.d + 1])
        K = self._k(self.X, self.X, ls, s2) + (n2 + 1e-8) * np.eye(len(self.X))
        try:
            L = np.linalg.cholesky(K)
        except np.linalg.LinAlgError:
            return 1e10
        a = np.linalg.solve(L.T, np.linalg.solve(L, self.yn))
        return 0.5 * self.yn @ a + np.log(np.diag(L)).sum()

    def _fit(self):
        from scipy.optimize import minimize
        best = None
        for ls0 in (0.2, 0.5, 1.0):
            th0 = np.r_[np.log(np.full(self.d, ls0)), 0.0, np.log(1e-2)]
            bnd = [(np.log(0.02), np.log(5))] * self.d + [(np.log(0.05), np.log(20)), (np.log(1e-6), np.log(0.5))]
            r = minimize(self._nll, th0, method='L-BFGS-B', bounds=bnd)
            if best is None or r.fun < best.fun:
                best = r
        th = best.x
        self.ls, self.s2, self.n2 = np.exp(th[:self.d]), np.exp(th[self.d]), np.exp(th[self.d + 1])
        K = self._k(self.X, self.X, self.ls, self.s2) + (self.n2 + 1e-8) * np.eye(len(self.X))
        self.L = np.linalg.cholesky(K)
        self.alpha = np.linalg.solve(self.L.T, np.linalg.solve(self.L, self.yn))

    def predict(self, Xs):
        Ks = self._k(np.asarray(Xs), self.X, self.ls, self.s2)
        m = Ks @ self.alpha
        v = np.linalg.solve(self.L, Ks.T)
        var = np.maximum(self.s2 - (v ** 2).sum(0), 1e-12)
        return m * self.sd + self.mu, np.sqrt(var) * self.sd


class Bayes(Optimizer):
    """Optimización bayesiana: GP con núcleo Matérn 5/2 y longitudes por variable (ARD) sobre log(dp0),
    criterio de mejora esperada (EI) maximizado sobre candidatos factibles + refinamiento local."""
    name = 'bayes'

    def __init__(self, rng, n_cand=3000, sp=None):
        super().__init__(rng, sp); self.n_cand = n_cand

    def ask(self):
        from scipy.stats import norm
        from scipy.optimize import minimize
        sp = self.sp; X = np.array(self.X); y = np.log(np.array(self.y))
        gp = GP(X, y); fbest = y.min()

        def ei(U):
            m, s = gp.predict(U); z = (fbest - m) / s
            return (fbest - m) * norm.cdf(z) + s * norm.pdf(z)
        pool = sp.feasible_pool()
        C = pool[self.rng.choice(len(pool), self.n_cand, replace=False)]
        # además, perturbaciones alrededor de los 5 mejores puntos (búsqueda local del criterio)
        loc = []
        for u in X[np.argsort(y)[:5]]:
            for _ in range(60):
                v = np.clip(u + self.rng.normal(0, 0.05, sp.dim), 0, 1)
                if sp.feasible(sp.to_params(v)):
                    loc.append(v)
        if loc:
            C = np.vstack([C, np.array(loc)])
        e = ei(C); top = C[np.argsort(-e)[:4]]
        best_u, best_e = C[int(np.argmax(e))], float(e.max())
        for u0 in top:
            f = lambda u: -ei(np.clip(u, 0, 1)[None])[0] + (0 if sp.feasible(sp.to_params(np.clip(u, 0, 1))) else 1e3)
            r = minimize(f, u0, method='Nelder-Mead', options={'maxfev': 80, 'xatol': 1e-3, 'fatol': 1e-6})
            u = np.clip(r.x, 0, 1)
            if -r.fun > best_e and sp.feasible(sp.to_params(u)):
                best_u, best_e = u, -r.fun
        self.last = {'ei': best_e, 'longitudes': dict(zip(sp.names, np.round(gp.ls, 3).tolist()))}
        return sp.snap(best_u)


# ------------------------------------------------------------------ CMA-ES
class CMA(Optimizer):
    """CMA-ES (librería cma) en el espacio normalizado; las muestras no factibles se vuelven a muestrear
    (sin coste de simulación). Arranca en el mejor punto inicial con sigma0 = 0,2."""
    name = 'cmaes'

    def __init__(self, rng, sigma0=0.2, sp=None):
        super().__init__(rng, sp); self.sigma0 = sigma0; self.es = None; self.batch = []; self.done = {}

    def _init(self):
        import cma
        x0, _ = self.best()
        self.es = cma.CMAEvolutionStrategy(list(x0), self.sigma0,
                                           {'bounds': [0, 1], 'seed': int(self.rng.integers(1, 2 ** 31)), 'verbose': -9})

    def ask(self):
        if self.es is None:
            self._init()
        if not self.batch:
            sols = []
            for _ in range(self.es.popsize):
                for _ in range(200):
                    s = np.clip(self.es.ask(1)[0], 0, 1)
                    if self.sp.feasible(self.sp.to_params(s)):
                        break
                sols.append(self.sp.snap(s))
            self.batch = sols; self.done = {}
        for i, s in enumerate(self.batch):
            if i not in self.done:
                self._cur = i
                return s
        return self.batch[0]

    def tell(self, u, y, info=None):
        super().tell(u, y, info)
        if self.es is None or not self.batch:
            return
        for i, s in enumerate(self.batch):
            if i not in self.done and np.allclose(s, u, atol=1e-6):
                self.done[i] = y; break
        if len(self.done) == len(self.batch):
            self.es.tell([self.batch[i] for i in range(len(self.batch))], [self.done[i] for i in range(len(self.batch))])
            self.batch = []


# ------------------------------------------------------------------ agente LLM
VAR_TEXT = {
    'rb': 'radio del labio (bellmouth) de entrada del runner',
    'R': 'radio de la curva del runner (eje), con D = 44 mm',
    'L2': 'tramo recto del runner antes de la mariposa',
    'x_rear': 'posición de la pared trasera del plenum; x crece hacia atrás, así que AUMENTAR x_rear (p. ej. de -45 a -40) aleja la pared, alarga el plenum y deja más sitio detrás del runner',
    'z_left': 'pared izquierda del plenum, lado opuesto al runner (más negativo = plenum más ancho)',
    'z_right': 'pared derecha del plenum, lado del runner (más positivo = plenum más ancho y más sitio para el labio)',
    'Lt': 'longitud de la transición conducto -> plenum (< 3 mm se construye como 0)',
    'r_plan': 'redondeo vertical de la esquina trasera izquierda del plenum',
    'c_sB': 'conducto, tramo entre la toma RAM y el chasis: factor de escala de la sección en el centro del tramo (1 = v5; >1 abomba, <1 estrecha; la boca oval no cambia)',
    'c_dy': 'conducto, tramo entre el chasis y el plenum: cuánto se sube el punto bajo (valle) del eje, en mm (0 = v5; suaviza la curva en S)',
    'c_sw': 'conducto, salida al plenum: factor de ancho de la boca (1 = v5, ~100 mm; >1 = difusor más ancho, menos velocidad al entrar al plenum)',
    'c_sh': 'conducto, salida al plenum: factor de alto de la boca (1 = v5, ~100 mm; el plenum mide 110 mm de alto)',
}

SYSTEM = """Eres un ingeniero de mecánica de fluidos que optimiza el airbox (caja de admisión) de una moto de
competición mediante simulaciones CFD (OpenFOAM, RANS k-omega SST, flujo estacionario 0,0336 kg/s).
Cada simulación cuesta unos 15 minutos de cálculo y tienes un presupuesto limitado: cada propuesta debe
contar. Tu objetivo es MINIMIZAR dp0_ext, la pérdida de presión total desde la toma hasta el final de una
extensión de 2 diámetros tras la mariposa (incluye la pérdida por mezcla aguas abajo de la curva).
Responde SIEMPRE con un único bloque JSON y nada más fuera de él."""


def describe_space(sp=None):
    sp = sp or BASE
    xr = dict((k, hi) for k, lo, hi in sp.vars)['x_rear']
    lines = [f'- {k}: {VAR_TEXT[k]}; rango [{lo:g}, {hi:g}] mm' for k, lo, hi in sp.vars]
    lab = ('el labio (rb) debe caber a lo ancho del plenum, entre z_left y z_right (fija en 80,8 mm)' if 'z_right' not in sp.names
           else 'el labio (rb) debe caber a lo ancho del plenum, entre z_left y z_right')
    return '\n'.join(lines) + f"""
Restricciones geométricas (una propuesta que no las cumpla se rechaza sin simular):
- la curva del runner debe caber entre el suelo del plenum y la mariposa (fija); R grande con L2 grande no cabe;
- el tubo del runner (D = 44) debe quedar delante de la pared trasera: su borde trasero <= x_rear - 2 mm;
  cuanto mayor es R, más atrás queda la entrada del runner, así que R grande exige x_rear cerca de {xr:g};
- {lab};
- r_plan <= 0,9 · min(largo, ancho) del plenum;
- volumen del plenum >= 2,6 L estimado (2,5 L reales, reglamento con margen)."""


def history_table(X, y, info, physical, sp=None):
    sp = sp or BASE
    rows = []
    for u, v, inf in sorted(zip(X, y, info), key=lambda t: t[1]):
        p = sp.to_params(u); r = {**p, 'dp0_ext': round(v, 1)}
        if physical and inf:
            for k in ('dp0_O', 'mezcla_ext', 'runner_O', 'plenum', 'conducto', 'backflow_O', 'vol_plenum_L'):
                if k in inf and inf[k] is not None:
                    r[k] = inf[k]
        rows.append(r)
    return json.dumps(rows, ensure_ascii=False)


PHYS_TEXT = """Significado de las columnas físicas de cada caso (Pa, salvo indicación):
- dp0_O: pérdida hasta el plano de la mariposa; mezcla_ext = dp0_ext - dp0_O, pérdida por mezcla aguas abajo
  (crece con la separación y la no uniformidad del flujo en la mariposa, típicamente por una curva cerrada);
- runner_O: pérdida desde la entrada del runner hasta la mariposa (labio + curva); plenum: dentro del plenum;
  conducto: conducto de entrada (casi constante, ~9,8 Pa);
- backflow_O: fracción de la sección de la mariposa con flujo invertido (separación);
- vol_plenum_L: volumen del plenum (L)."""

DEFAULT_MODEL = {'anthropic': 'claude-opus-5-5', 'ollama': 'gemma4:12b'}


def bridge_call(bridge_dir, req, timeout):
    """Puente por ficheros con el panel de Windows (que es quien llega a Ollama en localhost:11434 y a la API
    de Anthropic con la clave guardada en Windows): escribe peticiones/<id>.json y espera respuestas/<id>.json."""
    pet, resp = os.path.join(bridge_dir, 'peticiones'), os.path.join(bridge_dir, 'respuestas')
    os.makedirs(pet, exist_ok=True); os.makedirs(resp, exist_ok=True)
    rid = req['id']; tmp = os.path.join(pet, rid + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(req, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(pet, rid + '.json'))
    out = os.path.join(resp, rid + '.json'); t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.exists(out):
            time.sleep(0.5)
            with open(out, encoding='utf-8') as f:
                r = json.load(f)
            try:
                os.replace(out, os.path.join(resp, 'leidas_' + rid + '.json'))
            except OSError:
                pass
            if r.get('error'):
                raise RuntimeError(f'puente LLM: {r["error"]}')
            return r
        time.sleep(3)
    try:
        os.remove(os.path.join(pet, rid + '.json'))     # que no quede una petición huérfana al reintentar
    except OSError:
        pass
    raise RuntimeError(f'puente LLM: sin respuesta en {timeout} s (¿panel abierto? ¿Ollama/clave configurados?)')


class LLMAgent(Optimizer):
    """Agente LLM: recibe la descripción del problema y el historial y propone el siguiente diseño con un
    razonamiento breve. 'physical=True' añade el desglose de pérdidas de cada caso (LLM-físico).
    backend 'ollama' (modelo abierto, local) o 'anthropic'; con bridge_dir las llamadas las hace el panel."""

    def __init__(self, rng, physical=True, backend='ollama', model=None, temperature=None, log_path=None,
                 bridge_dir=None, num_ctx=24576, think=True, sp=None, run_id=''):
        super().__init__(rng, sp); self.physical = physical; self.backend = backend
        self.model = model or DEFAULT_MODEL[backend]
        self.temperature = temperature if temperature is not None else 1.0
        self.think = think
        self.log_path = log_path; self.bridge_dir = bridge_dir; self.num_ctx = num_ctx; self.run_id = run_id
        self.name = 'llm_fisico' if physical else 'llm_ciego'; self.n_calls = 0

    def _call(self, system, user):
        self.n_calls += 1
        seed = int(self.rng.integers(1, 2 ** 31))
        if self.bridge_dir:
            req = {'id': f'{self.run_id or "llm"}_{time.strftime("%Y%m%d%H%M%S")}_{self.n_calls:03d}', 'backend': self.backend,
                   'modelo': self.model, 'temperatura': self.temperature, 'semilla': seed, 'num_ctx': self.num_ctx, 'think': self.think,
                   'max_tokens': 8000, 'system': system, 'user': user, 't': time.strftime('%Y-%m-%d %H:%M:%S')}
            r = bridge_call(self.bridge_dir, req, 24 * 3600 if self.backend == 'ollama' else 900)
            return r.get('texto', ''), {**(r.get('uso') or {}), 'modelo_servido': r.get('modelo_servido'), 'digest': r.get('digest')}
        if self.backend == 'anthropic':
            key = os.environ.get('ANTHROPIC_API_KEY')
            if not key:
                raise RuntimeError('falta ANTHROPIC_API_KEY')
            body = {'model': self.model, 'max_tokens': 8000, 'temperature': self.temperature, 'system': system,
                    'messages': [{'role': 'user', 'content': user}]}
            req = urllib.request.Request('https://api.anthropic.com/v1/messages', data=json.dumps(body).encode(),
                                         headers={'x-api-key': key, 'anthropic-version': '2023-06-01',
                                                  'content-type': 'application/json'})
            r = json.loads(urllib.request.urlopen(req, timeout=300).read())
            return ''.join(c.get('text', '') for c in r.get('content', [])), r.get('usage', {})
        if self.backend == 'ollama':
            host = os.environ.get('OLLAMA_HOST_URL', 'http://localhost:11434')
            body = {'model': self.model, 'stream': False,
                    'options': {'temperature': self.temperature, 'seed': seed, 'num_ctx': self.num_ctx},
                    'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]}
            req = urllib.request.Request(host + '/api/chat', data=json.dumps(body).encode(), headers={'content-type': 'application/json'})
            r = json.loads(urllib.request.urlopen(req, timeout=3600).read())
            return r['message']['content'], {'prompt_tokens': r.get('prompt_eval_count'), 'output_tokens': r.get('eval_count')}
        raise RuntimeError(f'backend desconocido {self.backend}')

    def _prompt(self, budget_left, feedback=''):
        sp = self.sp
        txt = (f'Variables de diseño (mm):\n{describe_space(sp)}\n\n'
               + (PHYS_TEXT + '\n\n' if self.physical else '')
               + f'Casos ya simulados ({len(self.y)}), ordenados del mejor al peor:\n'
               + history_table(self.X, self.y, self.info, self.physical, sp)
               + f'\n\nTe quedan {budget_left} simulaciones. Propón el SIGUIENTE diseño a simular.'
               + ' Equilibra explorar zonas prometedoras poco vistas y explotar las mejores.'
               + (' Razona con la física del flujo (separación en el labio, curva, mezcla) a partir del desglose.' if self.physical else '')
               + '\nResponde solo con JSON: {"razonamiento": "<máx. 80 palabras>", "diseno": {'
               + ', '.join(f'"{k}": <número>' for k in sp.names) + '}}')
        return txt + (f'\n\nATENCIÓN: {feedback}' if feedback else '')

    @staticmethod
    def _parse(text):
        text = re.sub(r'<think>.*?</think>', '', text, flags=re.S)          # modelos con razonamiento visible (qwen3...)
        for m in reversed(list(re.finditer(r'\{(?:[^{}]|\{[^{}]*\})*\}', text, re.S))):
            try:
                d = json.loads(m.group(0))
                if isinstance(d, dict) and 'diseno' in d:
                    return d
            except Exception:
                continue
        raise ValueError('no hay un JSON con "diseno"')

    def ask(self, budget_left=40):
        sp = self.sp; feedback = ''
        for intento in range(4):
            user = self._prompt(budget_left, feedback)
            t0 = time.time()
            text, usage = self._call(SYSTEM, user)
            rec = {'t': time.strftime('%Y-%m-%d %H:%M:%S'), 'backend': self.backend, 'modelo': self.model,
                   'temperatura': self.temperature, 'intento': intento, 'prompt': user, 'respuesta': text,
                   'uso': usage, 's': round(time.time() - t0, 1)}
            p = None
            try:
                d = self._parse(text)
                p = {k: float(d['diseno'][k]) for k in sp.names}
                rec['razonamiento'] = d.get('razonamiento', '')
            except Exception as e:
                feedback = f'tu respuesta anterior no era JSON válido con todas las variables ({e}).'
            if p is not None:
                out = [k for k, lo, hi in sp.vars if not (lo - 1e-6 <= p[k] <= hi + 1e-6)]
                bad = sp.why_not(p) if not out else f'fuera de rango: {out}'
                if not bad:
                    u = sp.snap(sp.to_unit(p))
                    rep = [y for x, y in zip(self.X, self.y) if np.allclose(x, u, atol=1e-6)]
                    if rep:
                        bad = f'ese diseño ya está simulado (dp0_ext = {rep[0]:.1f} Pa); repetirlo no aporta información'
                if bad:
                    feedback = f'el diseño propuesto {json.dumps(p)} no es factible: {bad}. Propón otro.'; p = None
                rec['rechazo'] = bad
            if self.log_path:
                with open(self.log_path, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + '\n')
            if p is not None:
                self.last = {'razonamiento': rec.get('razonamiento', ''), 'intentos': intento + 1}
                return sp.snap(sp.to_unit(p))
        self.last = {'razonamiento': 'sin propuesta válida tras 4 intentos: punto aleatorio factible', 'intentos': 4}
        return sp.snap(sp.random_feasible(self.rng, 1)[0])


def make(method, rng, sp=None, **kw):
    if method == 'patron':
        return Pattern(rng, sp=sp)
    if method == 'bayes':
        return Bayes(rng, sp=sp)
    if method == 'cmaes':
        return CMA(rng, sp=sp)
    if method in ('llm_fisico', 'llm_ciego'):
        return LLMAgent(rng, physical=(method == 'llm_fisico'), sp=sp, **kw)
    raise ValueError(method)
