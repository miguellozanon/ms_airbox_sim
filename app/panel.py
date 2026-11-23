#!/usr/bin/env python3
"""Panel de simulaciones del airbox MS9: una página local en http://127.0.0.1:8780 .

Corre en Windows con el Python de Isaac Sim (solo biblioteca estándar), como los launchers del Go2 (8765),
del B2 (8770) y el Performance Lab de Formula Student (8090). El cálculo lo hace el trabajador
(lanzador.py) dentro de WSL Ubuntu-24.04, oculto y con prioridad mínima. Panel y trabajador hablan
solo a través de ficheros en C:\\ms_airbox\\cola.

  - estado del trabajador y progreso del caso en curso,
  - cola: añadir (formulario o lote JSON), reordenar, cancelar, reencolar,
  - resultados (tabla ordenable, CSV) y registro,
  - recursos: GPU, memoria, Isaac Sim / ROS 2 detectados y núcleos para OpenFOAM (normal / compartido),
  - arrancar, parar al acabar el caso, detener ya y apagar,
  - puente del agente LLM: el estudio (en WSL) deja peticiones en cola\\llm\\peticiones y el panel las pasa a
    Ollama (modelo abierto, http://127.0.0.1:11434; en espera mientras Isaac Sim esté abierto) o a la API de Anthropic (clave en C:\\ms_airbox\\secretos\\
    anthropic_api_key.txt o en la variable ANTHROPIC_API_KEY; nunca se escribe en registros ni respuestas).

Escucha solo en 127.0.0.1; las órdenes llevan una cabecera propia que ninguna otra web puede enviar.
Uso: Abrir_panel.bat (acceso directo «Airbox MS9»).  Opciones: --oculto  --arrancar-trabajador  --puerto N
"""
import csv, ctypes, glob, io, json, os, re, shlex, shutil, socket, subprocess, sys, threading, time, urllib.request, urllib.error, webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get('AIRBOX_BASE', os.path.dirname(HERE))          # C:\ms_airbox
QUEUE = os.environ.get('AIRBOX_QUEUE', os.path.join(BASE, 'cola'))
PORT = int(os.environ.get('AIRBOX_PORT', '8780'))
DISTRO = os.environ.get('AIRBOX_WSL_DISTRO', 'Ubuntu-24.04')
WAPP = os.environ.get('AIRBOX_WSL_APP', '/mnt/c/ms_airbox/app')
IS_WIN = os.name == 'nt'
NO_WINDOW = 0x08000000 if IS_WIN else 0
NEW_GROUP = 0x00000200 if IS_WIN else 0
WSL = shlex.split(os.environ['AIRBOX_WSL_PREFIX']) if 'AIRBOX_WSL_PREFIX' in os.environ else ['wsl', '-d', DISTRO, '--']   # pruebas fuera de Windows
FOLDERS = ('pendientes', 'en_curso', 'hechos', 'fallidos', 'cancelados')
NAME_RE = re.compile(r'^[A-Za-z0-9_\-]{1,40}$')
FILE_RE = re.compile(r'^[A-Za-z0-9_\-]{1,60}\.json$')
DEFAULT_PARAMS = {'rb': 10, 'R': 55, 'L2': 0, 'D': 44, 'x_rear': -40, 'z_left': -79.2, 'z_right': 80.8, 'Lt': 0, 'r_plan': 100, 'r_edge': 3, 'theta': 0}
SERVER = None
LOCK = threading.Lock()
ENV = {'isaac': [], 'gpu': {}, 'ram': {}, 'cpus': os.cpu_count()}
HB = {'mtime': None, 'mono': 0.0}
LLM_DIR = os.path.join(QUEUE, 'llm')
SECRETS = os.path.join(BASE, 'secretos')
LLM = {'ollama': None, 'modelos': [], 'version': None, 'anthropic_clave': False, 'atendidas': 0, 'en_curso': None,
       'ultima': None, 'error': None}

for d in FOLDERS:
    os.makedirs(os.path.join(QUEUE, d), exist_ok=True)


# ---------------------------------------------------------------- utilidades
def now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def rjson(path, default=None):
    for _ in range(3):
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except (OSError, ValueError):
            time.sleep(0.1)                 # el trabajador lo está reescribiendo
    return default


def wjson(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    for _ in range(15):
        try:
            os.replace(tmp, path); return
        except OSError:
            time.sleep(0.2)
    raise OSError(f'no puedo escribir {path}')


def listq(folder):
    return sorted(glob.glob(os.path.join(QUEUE, folder, '*.json')))


def log_panel(msg):
    with open(os.path.join(QUEUE, 'lanzador.log'), 'a', encoding='utf-8') as f:
        f.write(f'{now()}  [panel] {msg}\n')


def run_quiet(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
        return (r.stdout or b'').decode('utf-8', 'replace').replace('\x00', '').strip()
    except Exception as e:
        return f'error: {e}'


# ---------------------------------------------------------------- lo que pasa en el PC
OTHER_ISAAC_PS = (
    "Get-CimInstance Win32_Process | Where-Object { ($_.Name -in 'python.exe','kit.exe','isaac-sim.exe') -and "
    "($_.CommandLine -match 'isaacsim|isaac-sim|isaac_sim|isaaclab|kit\\.exe') -and "
    "($_.CommandLine -notmatch '_launcher\\.py|ms_airbox') } | ForEach-Object { $c = ($_.CommandLine -replace [char]34, '') "
    "-split ' ' | Where-Object { $_ -match '\\.py$|\\.kit$' } | Select-Object -First 1; if (-not $c) { $c = $_.Name }; "
    "'' + $_.ProcessId + ' ' + (Split-Path -Leaf $c) }")


def isaac_procs():
    """Isaac Sim / Isaac Lab abiertos en Windows (misma regla que el launcher del B2)."""
    if not IS_WIN:
        return []
    out = run_quiet(['powershell', '-NoProfile', '-NonInteractive', '-Command', OTHER_ISAAC_PS], 20)
    return [l.strip() for l in out.splitlines() if l.strip() and not l.startswith('error')]


def gpu_info():
    out = run_quiet(['nvidia-smi', '--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu',
                     '--format=csv,noheader,nounits'], 8)
    parts = [x.strip() for x in out.splitlines()[0].split(',')] if out and not out.startswith('error') else []
    if len(parts) < 5:
        return {}
    return {'nombre': parts[0], 'vram_usada_gb': round(float(parts[1]) / 1024, 1),
            'vram_total_gb': round(float(parts[2]) / 1024, 1), 'carga_pct': parts[3], 'temp_c': parts[4]}


def ram_info():
    if not IS_WIN:
        return {}
    class MS(ctypes.Structure):
        _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong), ('ullTotalPhys', ctypes.c_ulonglong),
                    ('ullAvailPhys', ctypes.c_ulonglong), ('ullTotalPageFile', ctypes.c_ulonglong),
                    ('ullAvailPageFile', ctypes.c_ulonglong), ('ullTotalVirtual', ctypes.c_ulonglong),
                    ('ullAvailVirtual', ctypes.c_ulonglong), ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
    m = MS(); m.dwLength = ctypes.sizeof(MS)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return {'total_gb': round(m.ullTotalPhys / 2**30, 1), 'libre_gb': round(m.ullAvailPhys / 2**30, 1), 'uso_pct': m.dwMemoryLoad}


def env_loop():
    """Cada 15 s: Isaac Sim, GPU y memoria. Se escribe en cola/entorno.json para que el trabajador decida núcleos."""
    while True:
        try:
            ENV.update(isaac=isaac_procs(), gpu=gpu_info(), ram=ram_info(), t=time.time(), actualizado=now())
            wjson(os.path.join(QUEUE, 'entorno.json'), ENV)
        except Exception:
            pass
        time.sleep(15)


# ---------------------------------------------------------------- puente LLM
_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(url, body=None, headers=None, timeout=10, local=False):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'content-type': 'application/json', **(headers or {})})
    op = _NOPROXY if local else urllib.request.build_opener()
    with op.open(req, timeout=timeout) as r:
        return json.loads(r.read())


def ollama_url():
    return os.environ.get('OLLAMA_HOST_URL', 'http://127.0.0.1:11434')


def anthropic_key():
    k = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    if not k:
        try:
            with open(os.path.join(SECRETS, 'anthropic_api_key.txt'), encoding='utf-8') as f:
                k = f.read().strip()
        except OSError:
            k = ''
    return k


def llm_status():
    try:
        tags = http_json(ollama_url() + '/api/tags', timeout=3, local=True)
        LLM['modelos'] = [{'nombre': m.get('name'), 'digest': (m.get('digest') or '')[:12],
                           'gb': round((m.get('size') or 0) / 2**30, 1)} for m in tags.get('models', [])]
        LLM['ollama'] = True
        try:
            LLM['version'] = http_json(ollama_url() + '/api/version', timeout=3, local=True).get('version')
        except Exception:
            pass
    except Exception:
        LLM['ollama'] = False; LLM['modelos'] = []
    LLM['anthropic_clave'] = bool(anthropic_key())
    try:                                            # para que el estudio no arranque agentes sin backend listo
        os.makedirs(LLM_DIR, exist_ok=True)
        wjson(os.path.join(LLM_DIR, 'estado.json'), {'t': time.time(), 'actualizado': now(), 'ollama': LLM['ollama'],
                                                     'modelos': [m['nombre'] for m in LLM['modelos']],
                                                     'anthropic_clave': LLM['anthropic_clave']})
    except Exception:
        pass


def llm_answer(req):
    """Una petición del estudio -> {'texto', 'uso', 'modelo_servido', 'digest', ...} o {'error'}."""
    be, model = req.get('backend'), req.get('modelo')
    if be == 'ollama':
        llm_status()
        if not LLM['ollama']:
            return {'error': 'Ollama no responde en 127.0.0.1:11434 (¿instalado y abierto?)'}
        names = {m['nombre']: m for m in LLM['modelos']}
        m = names.get(model) or names.get(model + ':latest')
        if not m:
            return {'error': f'falta el modelo {model}: ejecuta «ollama pull {model}» en Windows'}
        opts = {'temperature': req.get('temperatura', 1.0), 'seed': int(req.get('semilla', 1)), 'num_ctx': int(req.get('num_ctx', 24576))}
        body = {'model': model, 'stream': False, 'keep_alive': '2m', 'think': bool(req.get('think', True)), 'options': opts,
                'messages': [{'role': 'system', 'content': req.get('system', '')}, {'role': 'user', 'content': req.get('user', '')}]}
        r = http_json(ollama_url() + '/api/chat', body, timeout=3600, local=True)
        msg = r.get('message') or {}
        return {'texto': msg.get('content', ''), 'razonamiento_modelo': msg.get('thinking'),
                'uso': {'prompt_tokens': r.get('prompt_eval_count'), 'output_tokens': r.get('eval_count'),
                        'segundos': round((r.get('total_duration') or 0) / 1e9, 1)},
                'modelo_servido': r.get('model'), 'digest': m['digest'], 'ollama': LLM.get('version'),
                'temperatura': opts['temperature'], 'num_ctx': opts['num_ctx']}
    if be == 'anthropic':
        key = anthropic_key()
        if not key:
            return {'error': 'no hay clave de Anthropic: guárdala en C:\\ms_airbox\\secretos\\anthropic_api_key.txt'}
        body = {'model': model, 'max_tokens': int(req.get('max_tokens', 8000)), 'temperature': req.get('temperatura', 1.0),
                'system': req.get('system', ''), 'messages': [{'role': 'user', 'content': req.get('user', '')}]}
        try:
            r = http_json('https://api.anthropic.com/v1/messages', body, timeout=600,
                          headers={'x-api-key': key, 'anthropic-version': '2023-06-01'})
        except urllib.error.HTTPError as e:
            return {'error': f'API de Anthropic: HTTP {e.code} {e.read()[:300].decode("utf-8", "replace")}'}
        return {'texto': ''.join(c.get('text', '') for c in r.get('content', []) if c.get('type') == 'text'),
                'uso': r.get('usage', {}), 'modelo_servido': r.get('model')}
    return {'error': f'backend desconocido: {be}'}


def llm_loop(backend):
    """Atiende las peticiones de un backend, de una en una (Ollama y Anthropic en hilos separados)."""
    pet, resp, done = (os.path.join(LLM_DIR, d) for d in ('peticiones', 'respuestas', os.path.join('peticiones', 'atendidas')))
    for d in (pet, resp, done):
        os.makedirs(d, exist_ok=True)
    aviso = 0
    while True:
        try:
            if backend == 'ollama' and ENV.get('isaac'):
                # Isaac Sim abierto: la GPU y la RAM son suyas. Las peticiones esperan (el modelo de 12B no cabe
                # a la vez sin quitarle memoria); en cuanto se cierra, se atienden en orden.
                if glob.glob(os.path.join(pet, '*.json')) and time.time() - aviso > 3600:
                    aviso = time.time(); log_panel('LLM ollama: en espera mientras Isaac Sim esté abierto')
                LLM['en_curso'] = None; time.sleep(15); continue
            for f in sorted(glob.glob(os.path.join(pet, '*.json'))):
                req = rjson(f)
                if not req or req.get('backend') != backend:
                    continue
                rid = req.get('id') or os.path.basename(f)[:-5]
                LLM['en_curso'] = rid; t0 = time.time()
                try:
                    out = llm_answer(req)
                except Exception as e:
                    out = {'error': f'{type(e).__name__}: {e}'}
                out.update(id=rid, backend=backend, modelo=req.get('modelo'), s=round(time.time() - t0, 1), t=now())
                wjson(os.path.join(resp, rid + '.json'), out)
                try:
                    os.replace(f, os.path.join(done, os.path.basename(f)))
                except OSError:
                    pass
                LLM.update(en_curso=None, ultima=f'{now()} {backend} {req.get("modelo")} {out["s"]} s',
                           error=out.get('error'), atendidas=LLM['atendidas'] + 1)
                log_panel(f'LLM {backend} {req.get("modelo")}: {"ERROR " + out["error"] if out.get("error") else "respuesta"} en {out["s"]} s')
        except Exception as e:
            LLM['error'] = f'{type(e).__name__}: {e}'
        time.sleep(3)


def llm_status_loop():
    while True:
        try:
            llm_status()
        except Exception:
            pass
        time.sleep(60)


def heartbeat():
    """Estado que escribe el trabajador cada 5 s. «En marcha» = el fichero sigue cambiando (medido con el
    reloj monotónico de Windows, sin comparar horas de WSL y Windows, que pueden desfasarse)."""
    path = os.path.join(QUEUE, 'estado.json')
    hb = rjson(path, {}) or {}
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return hb, False
    first = HB['mtime'] is None
    if mt != HB['mtime']:
        HB.update(mtime=mt, mono=time.monotonic())
    alive = (time.monotonic() - HB['mono'] < 20) and (not first or time.time() - mt < 20)
    return hb, alive


def config():
    c = rjson(os.path.join(QUEUE, 'config.json'), {}) or {}
    hb, _ = heartbeat()
    cpus = hb.get('cpus') or ENV.get('cpus') or 4
    np_caso = int(c.get('np_caso') or 4)
    return {'np_caso': np_caso, 'casos_normal': int(c.get('casos_normal') or max(1, (cpus // 2) // np_caso)),
            'casos_compartido': int(c.get('casos_compartido') or 1),
            'pausar_con_isaac': bool(c.get('pausar_con_isaac', False)), 'cpus_wsl': cpus}


# ---------------------------------------------------------------- vistas
def job_summary(path):
    j = rjson(path, {}) or {}
    job = j.get('job', j)
    return {'file': os.path.basename(path), 'name': job.get('name'), 'params': job.get('params', {}),
            'h': job.get('h', 0.007), 'niter': job.get('niter', 1000), 'nota': job.get('nota', ''),
            'error': (j.get('error') or '')[-400:] if 'error' in j else None}


def all_names():
    names = set()
    for d in FOLDERS:
        for p in listq(d):
            j = rjson(p, {}) or {}
            names.add((j.get('job') or j).get('name'))
    return names


def next_prefix():
    nums = [int(m.group(1)) for p in listq('pendientes') if (m := re.match(r'^(\d+)_', os.path.basename(p)))]
    return (max(nums) + 10) if nums else 100


def results_rows():
    rows = []
    for p in listq('hechos'):
        r = rjson(p, {}) or {}
        job = r.get('job', {})
        row = {'archivo': os.path.basename(p), 'nombre': job.get('name'), 'estado': r.get('status'),
               'fin': r.get('fin', ''), 'h': job.get('h'), 'niter': job.get('niter'), 'np': r.get('np'),
               'celdas': r.get('celdas'), 'vol_plenum_L': r.get('vol_plenum_L'), 't_total_min': None}
        if r.get('t_total_s'):
            row['t_total_min'] = round(r['t_total_s'] / 60, 1)
        for k, v in (job.get('params') or {}).items():
            row['p_' + k] = v
        for k, v in (r.get('metrics') or {}).items():
            if k != 'caso':
                row[k] = v
        if r.get('nota'):
            row['nota'] = r['nota']
        rows.append(row)
    return rows


def status():
    hb, alive = heartbeat()
    trab = {t.get('archivo'): t for t in (hb.get('trabajos') or [])}
    actuales = []
    for p in listq('en_curso'):
        fn = os.path.basename(p); j = rjson(p, {}) or {}; t = trab.get(fn, {})
        a = {'file': fn, 'name': j.get('name'), 'params': j.get('params', {}), 'h': j.get('h', 0.007),
             'niter': j.get('niter', 1000), 'desde': t.get('desde'), 'fase': t.get('fase', '—'),
             'iter': t.get('iter', 0), 'np': t.get('np')}
        a['pct'] = round(100 * a['iter'] / max(a['niter'], 1), 1)
        actuales.append(a)
    done = [r for r in results_rows() if r.get('t_total_min') and r.get('estado') == 'done']
    t_med = round(sum(r['t_total_min'] for r in done[-10:]) / len(done[-10:]), 1) if done else None
    n_pend = len(listq('pendientes'))
    par = max(1, len(actuales))
    return {'worker': {'running': alive, 'host': hb.get('host'), 'cpus': hb.get('cpus'), 'nota': hb.get('nota'),
                       'ros2': hb.get('ros2'), 'wsl_mem_total_gb': hb.get('wsl_mem_total_gb'),
                       'wsl_mem_libre_gb': hb.get('wsl_mem_libre_gb'), 'actualizado': hb.get('actualizado')},
            'stop_pendiente': os.path.exists(os.path.join(QUEUE, 'STOP')),
            'actuales': actuales, 'mpi': hb.get('mpi', True), 'contadores': {d: len(listq(d)) for d in FOLDERS},
            't_medio_min': t_med, 'eta_cola_h': round((n_pend + len(actuales)) * t_med / 60 / par, 1) if t_med else None,
            'entorno': {k: ENV.get(k) for k in ('isaac', 'gpu', 'ram', 'actualizado')},
            'config': config(), 'ahora': now(), 'puerto': PORT, 'distro': DISTRO,
            'llm': {**LLM, 'pendientes': len(glob.glob(os.path.join(LLM_DIR, 'peticiones', '*.json')))}}


def tail(name, n=200):
    p = os.path.join(QUEUE, name)
    if not os.path.exists(p):
        return ''
    with open(p, 'rb') as f:
        f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 200000))
        lines = f.read().decode('utf-8', 'replace').splitlines()
    return '\n'.join(lines[-n:])


# ---------------------------------------------------------------- acciones
def add_jobs(jobs):
    names = all_names(); added, errors = [], []
    pref = next_prefix()
    for j in jobs:
        if not isinstance(j, dict):
            errors.append('cada caso debe ser un objeto JSON'); continue
        name = str(j.get('name', '')).strip()
        if not NAME_RE.match(name):
            errors.append(f'nombre no válido: "{name}" (letras, números, _ y -; máx. 40)'); continue
        if name in names:
            errors.append(f'ya existe un caso llamado {name}'); continue
        params = dict(DEFAULT_PARAMS); params.update(j.get('params') or {})
        try:
            params = {k: float(v) for k, v in params.items()}
            h = float(j.get('h', 0.007)); niter = int(j.get('niter', 1000))
        except (TypeError, ValueError):
            errors.append(f'{name}: algún valor no es numérico'); continue
        if not (0.002 <= h <= 0.02 and 200 <= niter <= 10000):
            errors.append(f'{name}: h debe estar entre 0,002 y 0,02 m y las iteraciones entre 200 y 10 000 (los resultados se guardan cada 100)'); continue
        job = {'name': name, 'params': params, 'h': h, 'niter': niter, 'keep_case': bool(j.get('keep_case', False))}
        if j.get('ignorar_volumen'):
            job['ignorar_volumen'] = True
        if j.get('nota'):
            job['nota'] = str(j['nota'])[:300]
        wjson(os.path.join(QUEUE, 'pendientes', f'{pref:04d}_{name}.json'), job)
        names.add(name); added.append(name); pref += 10
    if added:
        log_panel(f'añadidos {len(added)} casos: {", ".join(added[:10])}{"…" if len(added) > 10 else ""}')
    return {'ok': not errors, 'añadidos': added, 'errores': errors}


def move(file, src, dst):
    if not FILE_RE.match(file or '') or src not in FOLDERS or dst not in FOLDERS:
        return {'ok': False, 'error': 'petición no válida'}
    a = os.path.join(QUEUE, src, file)
    if not os.path.exists(a):
        return {'ok': False, 'error': f'{file} ya no está en {src}'}
    try:
        if dst == 'pendientes' and src in ('fallidos', 'cancelados'):
            j = rjson(a, {}) or {}
            job = j.get('job', j)                     # un fallido guarda el trabajo dentro de "job"
            wjson(os.path.join(QUEUE, 'pendientes', f'{next_prefix():04d}_{job.get("name")}.json'), job)
            os.remove(a)
        else:
            shutil.move(a, os.path.join(QUEUE, dst, file))
    except OSError as e:
        return {'ok': False, 'error': f'no se pudo mover ({e}); quizá el trabajador lo acaba de coger'}
    log_panel(f'{file}: {src} → {dst}')
    return {'ok': True}


def reorder(file, where):
    with LOCK:
        files = [os.path.basename(p) for p in listq('pendientes')]
        if file not in files:
            return {'ok': False, 'error': 'el caso ya no está pendiente (quizá ha empezado)'}
        i = files.index(file); files.pop(i)
        pos = {'arriba': 0, 'subir': max(0, i - 1), 'bajar': min(len(files), i + 1)}.get(where, len(files))
        files.insert(pos, file)
        for k, f in enumerate(files):
            new = f'{(k + 1) * 10:04d}_' + re.sub(r'^\d+_', '', f)
            if new != f:
                try:
                    os.replace(os.path.join(QUEUE, 'pendientes', f), os.path.join(QUEUE, 'pendientes', new))
                except OSError:
                    pass                               # el trabajador lo acaba de coger
    return {'ok': True}


def set_config(body):
    c = config()
    try:
        for k in ('np_caso', 'casos_normal', 'casos_compartido'):
            if k in body:
                c[k] = max(1, min(int(body[k]), 64))
        if 'pausar_con_isaac' in body:
            c['pausar_con_isaac'] = bool(body['pausar_con_isaac'])
    except (TypeError, ValueError):
        return {'ok': False, 'error': 'valor no válido'}
    c.pop('cpus_wsl', None)
    c['distro'] = DISTRO
    wjson(os.path.join(QUEUE, 'config.json'), c)
    log_panel(f'recursos: {c["np_caso"]} procesos por caso; {c["casos_normal"]} casos a la vez en modo normal, '
              f'{c["casos_compartido"]} en compartido; pausar con Isaac Sim: {"sí" if c["pausar_con_isaac"] else "no"}')
    return {'ok': True, 'msg': 'Guardado. Se aplica a los casos que empiecen a partir de ahora.'}


def detect_distro():
    """Distribución de WSL donde está instalado el entorno (~/airbox_env). Se guarda en cola/config.json."""
    global DISTRO, WSL
    if 'AIRBOX_WSL_DISTRO' in os.environ or 'AIRBOX_WSL_PREFIX' in os.environ or not IS_WIN:
        return DISTRO
    c = rjson(os.path.join(QUEUE, 'config.json'), {}) or {}
    names = [n.strip() for n in run_quiet(['wsl', '-l', '-q'], 15).splitlines() if n.strip()]
    order = ([c['distro']] if c.get('distro') in names else []) + [n for n in ('Ubuntu-24.04', 'Ubuntu') if n in names] + names
    for n in dict.fromkeys(order):
        if run_quiet(['wsl', '-d', n, '--', 'bash', '-c', 'test -x "$HOME/airbox_env/bin/python" && echo si'], 30) == 'si':
            DISTRO = n; WSL = ['wsl', '-d', DISTRO, '--']
            if c.get('distro') != n:
                c['distro'] = n; wjson(os.path.join(QUEUE, 'config.json'), c)
            return n
    return DISTRO


def start_worker():
    subprocess.Popen(WSL + ['bash', f'{WAPP}/arrancar_trabajador.sh'], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW | NEW_GROUP)


def stop_worker_now():
    return run_quiet(WSL + ['bash', f'{WAPP}/parar_trabajador.sh'], 30)


def worker_action(acc):
    _, alive = heartbeat(); stop = os.path.join(QUEUE, 'STOP')
    if acc == 'arrancar':
        if os.path.exists(stop):
            os.remove(stop)
        if alive:
            return {'ok': True, 'msg': 'el trabajador ya estaba en marcha'}
        start_worker(); log_panel('trabajador arrancado')
        return {'ok': True, 'msg': 'Arrancando el trabajador en WSL (tarda unos segundos)…'}
    if acc == 'parar':
        open(stop, 'w').close(); log_panel('parada pedida: termina el caso en curso y se detiene')
        return {'ok': True, 'msg': 'Se detendrá al terminar el caso en curso.'}
    if acc == 'cancelar_parada':
        if os.path.exists(stop):
            os.remove(stop)
        return {'ok': True, 'msg': 'Parada cancelada.'}
    if acc == 'matar':
        out = stop_worker_now(); HB['mono'] = -1e9      # no esperar 20 s a que caduque el latido
        caso = bool(listq('en_curso'))
        log_panel('trabajador detenido de inmediato' + ('; el caso en curso se repetirá al arrancar' if caso else '') + f' ({out})')
        return {'ok': True, 'msg': 'Detenido.' + (' El caso en curso se repetirá al volver a arrancar.' if caso else '')}
    if acc == 'apagar':
        if alive:
            stop_worker_now(); HB['mono'] = -1e9
        log_panel('panel apagado')
        threading.Timer(0.8, lambda: SERVER and SERVER.shutdown()).start()
        return {'ok': True, 'msg': 'Apagado.'}
    if acc == 'reiniciar_panel':
        # Recarga el código del panel sin tocar el trabajador (WSL, proceso aparte): un panel nuevo, oculto, toma el
        # puerto en cuanto este lo suelta (main() reintenta durante unos segundos).
        me, py, port, qlog = os.path.join(HERE, 'panel.py'), sys.executable, SERVER.server_address[1], os.path.join(QUEUE, 'panel_reinicio.log')

        def _restart():
            try:
                SERVER.shutdown(); SERVER.server_close()
            except Exception:
                pass
            DETACHED = 0x00000008 if IS_WIN else 0
            subprocess.Popen([py, me, '--oculto', '--puerto', str(port)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=open(qlog, 'a'),
                             creationflags=(DETACHED | NO_WINDOW | NEW_GROUP) if IS_WIN else 0, close_fds=True,
                             start_new_session=not IS_WIN)
            os._exit(0)
        log_panel('panel reiniciado para cargar código nuevo (el trabajador sigue)')
        t = threading.Timer(0.8, _restart); t.daemon = False; t.start()     # no demonio: debe sobrevivir al cierre del servidor
        return {'ok': True, 'msg': 'Reiniciando el panel (unos segundos)…'}
    return {'ok': False, 'error': 'acción desconocida'}


# ---------------------------------------------------------------- servidor
class H(BaseHTTPRequestHandler):
    server_version = 'AirboxPanel/2.0'

    def log_message(self, *a):
        pass

    def _host_ok(self):
        return (self.headers.get('Host') or '').split(':')[0] in ('localhost', '127.0.0.1')

    def _send(self, code, body, ctype='application/json; charset=utf-8', extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    STATIC_RE = re.compile(r'^(contexto/)?[A-Za-z0-9_][A-Za-z0-9_.\-]*\.(js|html|stl|json|txt)$')
    CTYPES = {'js': 'text/javascript; charset=utf-8', 'html': 'text/html; charset=utf-8', 'stl': 'application/octet-stream',
              'json': 'application/json; charset=utf-8', 'txt': 'text/plain; charset=utf-8'}

    def _static(self, rel):
        if not self.STATIC_RE.match(rel) or '..' in rel:
            return self._send(404, {'error': 'no existe'})
        f = os.path.join(HERE, 'static', *rel.split('/'))
        if not os.path.isfile(f):
            return self._send(404, {'error': 'no existe'})
        with open(f, 'rb') as fh:
            return self._send(200, fh.read(), self.CTYPES[rel.rsplit('.', 1)[1]])

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {'error': 'solo localhost'})
        u = urlparse(self.path); q = parse_qs(u.query)
        try:
            if u.path == '/':
                return self._send(200, PAGE, 'text/html; charset=utf-8')
            if u.path == '/api/estado':
                return self._send(200, status())
            if u.path == '/api/cola':
                return self._send(200, {d: [job_summary(p) for p in listq(d)] for d in ('pendientes', 'en_curso', 'fallidos', 'cancelados')})
            if u.path == '/api/resultados':
                return self._send(200, results_rows())
            if u.path == '/api/resultados.csv':
                rows = results_rows(); keys = []
                for r in rows:
                    keys += [k for k in r if k not in keys]
                s = io.StringIO(); w = csv.DictWriter(s, fieldnames=keys or ['nombre']); w.writeheader(); w.writerows(rows)
                return self._send(200, '\ufeff' + s.getvalue(), 'text/csv; charset=utf-8',
                                  {'Content-Disposition': 'attachment; filename="resultados_airbox.csv"'})
            if u.path == '/api/log':
                which = 'trabajador.log' if q.get('cual', [''])[0] == 'trabajador' else 'lanzador.log'
                return self._send(200, {'log': tail(which, int(q.get('n', ['200'])[0]))})
            if u.path == '/api/caso':
                f = q.get('file', [''])[0]; d = q.get('carpeta', ['hechos'])[0]
                if not FILE_RE.match(f) or d not in FOLDERS:
                    return self._send(400, {'error': 'petición no válida'})
                j = rjson(os.path.join(QUEUE, d, f))
                return self._send(200 if j is not None else 404, j if j is not None else {'error': 'no existe'})
            if u.path == '/vista':
                return self._static('vista.html')
            if u.path.startswith('/static/'):
                return self._static(u.path[len('/static/'):])
            if u.path == '/api/vista':
                n = q.get('caso', [''])[0]
                if not NAME_RE.match(n) and not re.match(r'^[A-Za-z0-9_\-]{1,60}$', n):
                    return self._send(400, {'error': 'caso no válido'})
                d = os.path.join(QUEUE, 'vista'); f = os.path.join(d, n + '.stl')
                if os.path.exists(f):
                    with open(f, 'rb') as fh:
                        return self._send(200, fh.read(), 'application/octet-stream')
                if os.path.exists(os.path.join(d, n + '.err')):
                    with open(os.path.join(d, n + '.err'), encoding='utf-8') as fh:
                        return self._send(404, {'error': fh.read()[:300]})
                pd = os.path.join(d, 'peticiones'); os.makedirs(pd, exist_ok=True)
                open(os.path.join(pd, n + '.req'), 'w').close()
                return self._send(202, {'pendiente': True, 'trabajador': heartbeat()[1]})
            if u.path == '/api/diag':                     # diagnóstico de solo lectura del trabajador (app/diag.sh)
                try:
                    r = subprocess.run(WSL + ['bash', f'{WAPP}/diag.sh'], capture_output=True, text=True, timeout=60,
                                       creationflags=NO_WINDOW, encoding='utf-8', errors='replace')
                    return self._send(200, {'salida': (r.stdout or '') + (r.stderr or '')})
                except Exception as e:
                    return self._send(500, {'error': str(e)})
            if u.path == '/api/defaults':
                return self._send(200, {'params': DEFAULT_PARAMS, 'h': 0.007, 'niter': 1000})
            return self._send(404, {'error': 'no existe'})
        except Exception as e:
            return self._send(500, {'error': str(e)})

    def do_POST(self):
        # La cabecera propia obliga al navegador a pedir permiso CORS, que no se da: ninguna web ajena puede mandar órdenes.
        if not self._host_ok() or self.headers.get('X-Airbox') != '1':
            return self._send(403, {'error': 'petición rechazada'})
        try:
            n = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(n) or b'{}') if n else {}
            p = urlparse(self.path).path
            if p == '/api/trabajos':
                jobs = body.get('jobs') if isinstance(body.get('jobs'), list) else [body]
                return self._send(200, add_jobs(jobs))
            if p == '/api/mover':
                return self._send(200, move(body.get('file'), body.get('de'), body.get('a')))
            if p == '/api/orden':
                return self._send(200, reorder(body.get('file'), body.get('donde')))
            if p == '/api/trabajador':
                return self._send(200, worker_action(body.get('accion')))
            if p == '/api/config':
                return self._send(200, set_config(body))
            return self._send(404, {'error': 'no existe'})
        except Exception as e:
            return self._send(500, {'ok': False, 'error': str(e)})


PAGE = r"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Airbox MS9 · simulaciones</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--ink:#1d2330;--quiet:#5d6677;--line:#e0e3e8;--soft:#eef0f3;--acc:#c2410c;--acc-soft:#fff1e8;--on-acc:#fff;
--ok:#16794a;--ok-soft:#e5f5ec;--warn:#9a6700;--warn-soft:#fff4d6;--bad:#b42318;--bad-soft:#fde8e7;--bar:#2563eb;--mono:"Cascadia Mono",Consolas,ui-monospace,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1c2027;--ink:#e7eaf0;--quiet:#9aa3b2;--line:#2c323c;--soft:#232831;--acc:#fb8a4c;--acc-soft:#3a2416;--on-acc:#1a0d05;
--ok:#4cc38a;--ok-soft:#173326;--warn:#f0b72f;--warn-soft:#3a2f12;--bad:#ff7b72;--bad-soft:#3d1c1a;--bar:#6ea0ff}}
*{box-sizing:border-box}[hidden]{display:none!important}body{margin:0;font:14px/1.45 "Segoe UI",system-ui,sans-serif;background:var(--bg);color:var(--ink)}
header{display:flex;align-items:center;gap:14px;padding:10px 22px;border-bottom:1px solid var(--line);background:var(--card);position:sticky;top:0;z-index:3;flex-wrap:wrap}
header .logo{width:30px;height:30px;border-radius:8px;background:var(--acc);display:grid;place-items:center;flex:none}
header h1{font-size:16px;margin:0;font-weight:650}header h1 span{color:var(--quiet);font-weight:400}
header .sub{color:var(--quiet);font-size:12px}
.pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:99px;font-size:12.5px;font-weight:600;white-space:nowrap}
.pill.on{background:var(--ok-soft);color:var(--ok)}.pill.off{background:var(--bad-soft);color:var(--bad)}.pill.wait{background:var(--warn-soft);color:var(--warn)}.pill.info{background:var(--soft);color:var(--quiet)}
.dot{width:8px;height:8px;border-radius:50%;background:currentColor}
main{max-width:1320px;margin:0 auto;padding:18px 22px 60px;display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;min-width:0}
.card h2{font-size:14px;margin:0 0 12px;font-weight:650;display:flex;align-items:center;gap:8px}.card h2 .n{color:var(--quiet);font-weight:500}
.full{grid-column:1/-1}.grow{flex:1}
.kpis{display:flex;gap:26px;flex-wrap:wrap;margin-bottom:10px}.kpi b{display:block;font-size:19px;font-weight:650}.kpi span{color:var(--quiet);font-size:12px}
.bar{height:8px;background:var(--soft);border-radius:4px;overflow:hidden;margin:8px 0 4px}.bar>div{height:100%;background:var(--bar);transition:width .4s}
.muted{color:var(--quiet)}.small{font-size:12.5px}
button,.btn{font:inherit;font-size:13px;padding:6px 12px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer;text-decoration:none;display:inline-block}
button:hover,.btn:hover{border-color:var(--acc)}button.primary{background:var(--acc);border-color:var(--acc);color:var(--on-acc);font-weight:600}
button.danger{color:var(--bad)}button.armed{background:var(--bad);border-color:var(--bad);color:#fff}button.icon{padding:3px 8px;font-size:12px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{color:var(--quiet);font-weight:600;font-size:12px;cursor:pointer;user-select:none;position:sticky;top:0;background:var(--card)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}.scroll{overflow:auto;max-height:420px}
.best td{background:var(--ok-soft)}.mini{display:inline-block;height:8px;background:var(--bar);border-radius:2px;vertical-align:middle;opacity:.7}
form .grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px 12px;margin-bottom:12px}
label{display:flex;flex-direction:column;font-size:12px;color:var(--quiet);gap:3px}label.chk{flex-direction:row;align-items:center;gap:8px;font-size:13px;color:var(--ink)}
input,textarea,select{width:100%;min-width:0;font:inherit;font-size:13px;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink)}
input[type=checkbox]{width:auto}
textarea{min-height:120px;font-family:var(--mono);font-size:12px}
pre{margin:0;font:12px/1.5 var(--mono);white-space:pre-wrap;word-break:break-word;max-height:340px;overflow:auto;background:var(--bg);border-radius:6px;padding:10px}
.tabs{display:flex;gap:4px;margin-bottom:10px}.tabs button{border-radius:6px}.tabs button.sel{background:var(--acc-soft);border-color:var(--acc);color:var(--acc)}
.res{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:12px}
.res div{background:var(--soft);border-radius:8px;padding:8px 10px}.res b{display:block;font-size:15px}.res span{font-size:11.5px;color:var(--quiet)}
.note{font-size:12.5px;color:var(--quiet);margin-top:8px}
#toast{position:fixed;right:20px;bottom:20px;max-width:460px;padding:10px 14px;border-radius:8px;background:var(--ink);color:var(--bg);font-size:13px;opacity:0;transition:opacity .25s;pointer-events:none;z-index:9}
#toast.show{opacity:.96}.err{color:var(--bad)}
@media (max-width:980px){main{grid-template-columns:1fr}.res{grid-template-columns:repeat(2,minmax(0,1fr))}form .grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body>
<header><div class="logo"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"><path d="M3 8h9a4 4 0 0 1 4 4v8"/><path d="M3 13h6a2 2 0 0 1 2 2v5"/><path d="M19 4c1.5 1 2 2.5 2 4"/></svg></div>
<div><h1>Airbox MS9 <span>· simulaciones OpenFOAM (WSL)</span></h1><div class="sub" id="host">—</div></div>
<span id="wpill" class="pill wait"><span class="dot"></span><span id="wtext">…</span></span>
<span id="mode" class="pill info" hidden></span>
<span class="grow"></span>
<div class="row">
<button id="bstart" class="primary" data-acc="arrancar">Arrancar</button>
<button id="bstop" data-acc="parar">Parar al acabar el caso</button>
<button id="bkill" class="danger" data-acc="matar">Detener ya</button>
<a class="btn" href="/vista" target="_blank" title="Ver en 3D la forma del volumen de aire de cada diseño, sobre la moto">Visor 3D</a>
<button id="boff" data-acc="apagar" title="Detiene el cálculo y cierra el panel">Apagar</button>
</div></header>
<main>
<section class="card full"><h2>Casos en curso</h2><div id="actualbody" class="muted">—</div></section>

<section class="card"><h2>Cola <span class="n" id="npend"></span></h2>
<div class="tabs" id="qtabs"><button class="sel" data-q="pendientes">Pendientes</button><button data-q="fallidos">Fallidos</button><button data-q="cancelados">Cancelados</button></div>
<div class="scroll" style="max-height:360px"><table id="qtable"></table></div></section>

<section class="card"><h2>Añadir casos</h2>
<div class="tabs" id="ftabs"><button class="sel" data-f="uno">Un caso</button><button data-f="lote">Lote JSON</button></div>
<form id="f1"><div class="grid">
<label>Nombre<input name="name" required pattern="[A-Za-z0-9_\-]{1,40}" placeholder="p.ej. rb12_R60"></label>
<label>Labio runner r_b (mm)<input name="rb" type="number" step="any"></label>
<label>Radio curva R (mm)<input name="R" type="number" step="any"></label>
<label>Tramo recto L2 (mm)<input name="L2" type="number" step="any"></label>
<label>Diámetro runner D (mm)<input name="D" type="number" step="any" title="Fijo en 44 (mariposa Keihin)"></label>
<label>Pared trasera x_rear (mm)<input name="x_rear" type="number" step="any"></label>
<label>Pared izquierda z_left (mm)<input name="z_left" type="number" step="any"></label>
<label>Pared derecha z_right (mm)<input name="z_right" type="number" step="any"></label>
<label>Transición L_t (mm)<input name="Lt" type="number" step="any"></label>
<label>Redondeo planta r_plan (mm)<input name="r_plan" type="number" step="any"></label>
<label>Borde r_edge (mm)<input name="r_edge" type="number" step="any"></label>
<label>Inclinación θ (°)<input name="theta" type="number" step="any"></label>
<label>Tamaño de celda h (m)<select name="h"><option value="0.007">0,007 · gruesa (~0,3 M)</option><option value="0.005">0,005 · media</option><option value="0.0035">0,0035 · fina (~1,7 M)</option></select></label>
<label>Iteraciones<input name="niter" type="number" step="100" min="200"></label>
<label>Guardar caso completo<select name="keep_case"><option value="false">No (solo métricas)</option><option value="true">Sí</option></select></label>
</div><div class="row"><button class="primary" type="submit">Añadir a la cola</button><span class="muted small">Valores por defecto: variante D</span></div></form>
<form id="f2" hidden><textarea name="lote" spellcheck="false" placeholder='[{"name":"rb12","params":{"rb":12}},{"name":"R60","params":{"R":60},"h":0.007}]'></textarea>
<div class="row" style="margin-top:8px"><button class="primary" type="submit">Añadir lote</button><span class="muted small">Los parámetros que no pongas toman el valor de D.</span></div></form>
<div id="ferr" class="err small" style="margin-top:8px"></div></section>

<section class="card full"><h2>Resultados <span class="n" id="nres"></span><span class="grow"></span>
<select id="metric" style="width:auto" title="Métrica para la barra y el mejor caso"><option value="dp0_ext">Δp0 con mezcla (Pa)</option><option value="dp0_O">Δp0 hasta mariposa (Pa)</option></select>
<a class="btn" href="/api/resultados.csv">Descargar CSV</a></h2>
<div class="scroll"><table id="rtable"></table></div></section>

<section class="card"><h2>Recursos y convivencia</h2>
<div class="res" id="res"></div>
<form id="fcfg"><div class="grid" style="grid-template-columns:repeat(2,minmax(0,1fr))">
<label>Casos a la vez · PC libre<input name="casos_normal" type="number" min="1" step="1"></label>
<label>Casos a la vez · con Isaac Sim o ROS 2<input name="casos_compartido" type="number" min="1" step="1"></label>
<label>Procesos por caso (fijo)<input name="np_caso" type="number" min="1" step="1" title="Cambiarlo cambia ligeramente la malla: déjalo igual en todo un estudio"></label>
<div class="small muted" style="align-self:end">Todos los casos usan los mismos procesos para que sean comparables; la carga del PC solo cambia cuántos van a la vez.</div>
</div><label class="chk"><input type="checkbox" name="pausar_con_isaac"> No empezar casos nuevos mientras Isaac Sim esté abierto</label>
<div class="row" style="margin-top:10px"><button class="primary" type="submit">Guardar</button><span class="muted small">Se aplica a los casos que empiecen a partir de ahora. OpenFOAM corre siempre con prioridad mínima.</span></div></form></section>

<section class="card"><h2>Registro<span class="grow"></span><span class="tabs" id="ltabs" style="margin:0"><button class="sel" data-l="cola">Cola</button><button data-l="trabajador">WSL</button></span></h2><pre id="log"></pre></section>
</main><div id="toast"></div>
<script>
const $=s=>document.querySelector(s);let OFF=false,Q='pendientes',L='cola',SORT={k:'fin',d:-1},RES=[],DEF={},CFG_DIRTY=false;
const fmt=(v,d=1)=>v==null||v===''?'—':(typeof v==='number'?v.toLocaleString('es-ES',{maximumFractionDigits:d}):v);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(m,bad){const t=$('#toast');t.textContent=m;t.style.background=bad?'var(--bad)':'';t.classList.add('show');clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('show'),4500)}
async function get(u){const r=await fetch(u,{cache:'no-store'});return r.json()}
async function post(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json','X-Airbox':'1'},body:JSON.stringify(b||{})});return r.json()}
function arm(btn,fn){if(btn.classList.contains('armed')){btn.classList.remove('armed');btn.textContent=btn._t;fn();return}
 btn._t=btn.textContent;btn.classList.add('armed');btn.textContent='¿Seguro? Pulsa otra vez';setTimeout(()=>{if(btn.classList.contains('armed')){btn.classList.remove('armed');btn.textContent=btn._t}},4000)}
function bye(){OFF=true;document.body.innerHTML='<div style="padding:48px;font:16px Segoe UI,system-ui"><h2>Panel apagado</h2><p>El cálculo se ha detenido. Para volver a abrirlo: <b>Airbox MS9</b> en <code>C:\\ms_airbox</code>.</p></div>'}
async function worker(acc,btn){const go=async()=>{const r=await post('/api/trabajador',{accion:acc});toast(r.msg||r.error,!r.ok);if(acc==='apagar'&&r.ok){setTimeout(bye,600);return}setTimeout(refresh,1500)};(acc==='matar'||acc==='apagar')?arm(btn,go):go()}
document.querySelectorAll('header button').forEach(b=>b.onclick=()=>worker(b.dataset.acc,b));
function params(p){return Object.entries(p||{}).filter(([k,v])=>DEF.params&&DEF.params[k]!==v).map(([k,v])=>`${k}=${fmt(v,2)}`).join(' · ')||'= variante D'}
function drawRes(s){const e=s.entorno||{},g=e.gpu||{},r=e.ram||{},w=s.worker,c=s.config,isaac=(e.isaac||[]).length,L=s.llm||{modelos:[]};
 $('#res').innerHTML=`<div><b>${isaac?'Abierto':'No'}</b><span>Isaac Sim / Isaac Lab${isaac?': '+esc(e.isaac.map(x=>x.split(' ').slice(1).join(' ')).join(', ')):''}</span></div>
 <div><b>${w.ros2==null?'—':(w.ros2?'En marcha':'No')}</b><span>ROS 2 en WSL</span></div>
 <div><b>${r.total_gb?fmt(r.libre_gb)+' / '+fmt(r.total_gb)+' GB':'—'}</b><span>RAM libre en Windows${w.wsl_mem_total_gb?` · WSL ${fmt(w.wsl_mem_libre_gb)} / ${fmt(w.wsl_mem_total_gb)} GB`:''}</span></div>
 <div><b>${g.nombre?fmt(g.vram_usada_gb)+' / '+fmt(g.vram_total_gb)+' GB':'—'}</b><span>${g.nombre?esc(g.nombre)+' · '+g.carga_pct+' % · '+g.temp_c+' °C':'GPU (nvidia-smi)'}</span></div>
 <div><b>${L.ollama?(L.modelos.length?esc(L.modelos.map(m=>m.nombre).join(', ')):'Ollama sin modelos'):(L.ollama===false?'Ollama no instalado / cerrado':'—')}</b><span>Agente LLM · Anthropic: ${L.anthropic_clave?'clave guardada':'sin clave'}${L.en_curso?' · respondiendo '+esc(L.en_curso):''}${L.pendientes?' · '+L.pendientes+' en espera':''}${L.error?' · <span class="err">'+esc(L.error.slice(0,120))+'</span>':''}</span></div>`;
 const f=$('#fcfg');if(!CFG_DIRTY){f.casos_normal.value=c.casos_normal;f.casos_compartido.value=c.casos_compartido;f.np_caso.value=c.np_caso;f.pausar_con_isaac.checked=c.pausar_con_isaac}
 const m=$('#mode');m.hidden=!w.running;const shared=isaac||w.ros2;const np=s.mpi===false?1:c.np_caso;m.textContent=w.nota&&w.nota.startsWith('en pausa')?'En pausa por Isaac Sim':(shared?`Compartido · ${c.casos_compartido} a la vez × ${np} proc.`:`PC libre · ${c.casos_normal} a la vez × ${np} proc.`)+(s.mpi===false?' · en serie (MPI)':'')}
async function refreshStatus(){const s=await get('/api/estado'),w=s.worker,pill=$('#wpill');
 $('#host').textContent=`${s.distro} · ${w.cpus||'?'} hilos en WSL · http://127.0.0.1:${s.puerto} · ${s.ahora}`;
 if(w.running&&s.stop_pendiente){pill.className='pill wait';$('#wtext').textContent='Parará al acabar el caso'}
 else if(w.running){pill.className='pill on';$('#wtext').textContent=w.nota&&w.nota.startsWith('en pausa')?'En pausa':'Trabajando'}else{pill.className='pill off';$('#wtext').textContent='Detenido'}
 $('#bstart').hidden=w.running&&!s.stop_pendiente;$('#bstart').textContent=w.running?'Cancelar la parada':'Arrancar';$('#bstart').dataset.acc=w.running?'cancelar_parada':'arrancar';
 $('#bstop').hidden=!w.running||s.stop_pendiente;$('#bkill').hidden=!w.running;
 const c=s.contadores;$('#npend').textContent=`· ${c.pendientes} pendientes · ${c.fallidos} fallidos`;drawRes(s);
 const A=s.actuales||[],el=$('#actualbody');
 const foot=`<div class="kpis" style="margin:10px 0 0"><div class="kpi"><b>${fmt(s.t_medio_min)} min</b><span>media por caso</span></div><div class="kpi"><b>${s.eta_cola_h==null?'—':fmt(s.eta_cola_h)+' h'}</b><span>para vaciar la cola (al ritmo actual)</span></div></div>`;
 if(!A.length){el.innerHTML=`<span class="muted">${w.running?(w.nota&&w.nota.startsWith('en pausa')?'En pausa: Isaac Sim está abierto. Los casos pendientes esperan.':'Esperando casos nuevos en la cola.'):'No hay ningún caso en marcha.'}</span>`+(s.t_medio_min?foot:'');return}
 el.innerHTML='<table><tr><th>Caso</th><th>Parámetros</th><th>Fase</th><th style="width:30%">Iteraciones</th><th class="num">Proc.</th><th>Empezó</th></tr>'+A.map(a=>`<tr><td><b>${esc(a.name)}</b></td><td class="small">${esc(params(a.params))}</td><td>${w.running?esc(a.fase):'<span class="err">interrumpido</span>'}</td><td><div class="bar" style="margin:0"><div style="width:${a.pct}%"></div></div><span class="small muted">${a.iter} / ${a.niter}</span></td><td class="num">${a.np||'—'}</td><td class="small muted">${esc((a.desde||'—').slice(11))}</td></tr>`).join('')+'</table>'+(w.running?'':'<div class="note">Se repetirán al volver a arrancar.</div>')+foot}
async function refreshQueue(){const q=await get('/api/cola'),rows=q[Q]||[],t=$('#qtable');
 if(!rows.length){t.innerHTML=`<tr><td class="muted">Sin casos ${Q}.</td></tr>`;return}
 t.innerHTML='<tr><th>Caso</th><th>Parámetros</th><th class="num">h</th><th></th></tr>'+rows.map(r=>{
  const act=Q==='pendientes'?`<button class="icon" title="Primero" data-o="arriba">⤒</button><button class="icon" title="Subir" data-o="subir">↑</button><button class="icon" title="Bajar" data-o="bajar">↓</button><button class="icon danger" data-m="cancelados">Cancelar</button>`
   :`<button class="icon" data-m="pendientes">Reencolar</button>`;
  return `<tr data-f="${esc(r.file)}"><td><b>${esc(r.name)}</b>${r.error?`<div class="err small" title="${esc(r.error)}">${esc(r.error.slice(-90))}</div>`:''}</td><td class="small">${esc(params(r.params))}</td><td class="num">${r.h}</td><td class="row" style="justify-content:flex-end">${act}</td></tr>`}).join('');
 t.querySelectorAll('button').forEach(b=>b.onclick=async()=>{const f=b.closest('tr').dataset.f;
  const r=b.dataset.o?await post('/api/orden',{file:f,donde:b.dataset.o}):await post('/api/mover',{file:f,de:Q,a:b.dataset.m});
  if(!r.ok)toast(r.error,true);refreshQueue()})}
$('#qtabs').onclick=e=>{if(!e.target.dataset.q)return;Q=e.target.dataset.q;document.querySelectorAll('#qtabs button').forEach(b=>b.classList.toggle('sel',b===e.target));refreshQueue()};
$('#ltabs').onclick=e=>{if(!e.target.dataset.l)return;L=e.target.dataset.l;document.querySelectorAll('#ltabs button').forEach(b=>b.classList.toggle('sel',b===e.target));refreshLog()};
const COLS=[['nombre','Caso'],['dp0_ext','Δp0 mezcla (Pa)',1],['dp0_O','Δp0 mariposa (Pa)',1],['mezcla_ext','Mezcla (Pa)',1],['runner_O','Runner (Pa)',1],['plenum','Plenum (Pa)',1],['conducto','Conducto (Pa)',1],
 ['backflow_O','Flujo inv.',3],['vol_plenum_L','Plenum (L)',2],['celdas','Celdas',0],['np','Proc.',0],['t_total_min','Tiempo (min)',1],['p_rb','r_b',1],['p_R','R',1],['p_L2','L2',1],['p_x_rear','x_rear',1],['p_z_left','z_left',1],['p_z_right','z_right',1],['p_c_sB','c_sB',2],['p_c_dy','c_dy',1],['p_c_sw','c_sw',2],['p_c_sh','c_sh',2],['p_Lt','L_t',1],['p_r_plan','r_plan',1],['h','h',4],['fin','Terminado']];
async function refreshResults(){RES=await get('/api/resultados');drawResults()}
function drawResults(){const m=$('#metric').value,t=$('#rtable');$('#nres').textContent=`· ${RES.length}`;
 if(!RES.length){t.innerHTML='<tr><td class="muted">Todavía no hay resultados.</td></tr>';return}
 const vals=RES.map(r=>r[m]).filter(v=>typeof v==='number'),mx=Math.max(...vals,1),mn=Math.min(...vals);
 const rows=[...RES].sort((a,b)=>{const x=a[SORT.k],y=b[SORT.k];if(x==null)return 1;if(y==null)return -1;return(x>y?1:x<y?-1:0)*SORT.d});
 t.innerHTML='<tr>'+COLS.map(([k,l,d])=>`<th class="${d!=null?'num':''}" data-k="${k}">${l}${SORT.k===k?(SORT.d>0?' ▲':' ▼'):''}</th>`).join('')+'<th></th></tr>'+
 rows.map(r=>`<tr class="${r[m]===mn&&r.estado==='done'?'best':''}">`+COLS.map(([k,l,d])=>{let v=r[k];
  if(k===m&&typeof v==='number')return `<td class="num">${fmt(v,d)} <span class="mini" style="width:${Math.round(60*v/mx)}px"></span></td>`;
  if(k==='nombre')return `<td><b>${esc(v)}</b>${r.estado!=='done'?` <span class="muted small">(${esc(r.estado)}${r.nota?': '+esc(r.nota):''})</span>`:''}</td>`;
  return `<td class="${d!=null?'num':''}">${fmt(v,d)}</td>`}).join('')+`<td>${r.estado==='done'?`<a class="btn icon" href="/vista?a=${encodeURIComponent(r.nombre)}" target="_blank">3D</a> `:''}<button class="icon" data-f="${esc(r.archivo)}">JSON</button></td></tr>`).join('');
 t.querySelectorAll('th[data-k]').forEach(th=>th.onclick=()=>{const k=th.dataset.k;SORT={k,d:SORT.k===k?-SORT.d:1};drawResults()});
 t.querySelectorAll('button[data-f]').forEach(b=>b.onclick=()=>window.open('/api/caso?carpeta=hechos&file='+encodeURIComponent(b.dataset.f)))}
$('#metric').onchange=drawResults;
async function refreshLog(){const r=await get('/api/log?n=150&cual='+L),p=$('#log'),bottom=p.scrollTop+p.clientHeight>=p.scrollHeight-20;p.textContent=r.log||'(vacío)';if(bottom)p.scrollTop=p.scrollHeight}
$('#ftabs').onclick=e=>{if(!e.target.dataset.f)return;document.querySelectorAll('#ftabs button').forEach(b=>b.classList.toggle('sel',b===e.target));$('#f1').hidden=e.target.dataset.f!=='uno';$('#f2').hidden=e.target.dataset.f!=='lote'};
async function submitJobs(jobs){const r=await post('/api/trabajos',{jobs});$('#ferr').innerHTML=(r.errores||[]).map(esc).join('<br>')||(r.error?esc(r.error):'');
 if(r.añadidos&&r.añadidos.length){toast(`Añadidos: ${r.añadidos.join(', ')}`);refreshQueue();refreshStatus()}return r}
$('#f1').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target),p={};for(const k of Object.keys(DEF.params))p[k]=Number(f.get(k));
 const r=await submitJobs([{name:f.get('name'),params:p,h:Number(f.get('h')),niter:Number(f.get('niter')),keep_case:f.get('keep_case')==='true'}]);if(r.ok)e.target.name.value=''};
$('#f2').onsubmit=async e=>{e.preventDefault();let jobs;try{jobs=JSON.parse(e.target.lote.value);if(!Array.isArray(jobs))jobs=[jobs]}catch(x){$('#ferr').textContent='JSON no válido: '+x.message;return}
 const r=await submitJobs(jobs);if(r.ok)e.target.lote.value=''};
$('#fcfg').oninput=()=>{CFG_DIRTY=true};
$('#fcfg').onsubmit=async e=>{e.preventDefault();const f=e.target;const r=await post('/api/config',{casos_normal:Number(f.casos_normal.value),casos_compartido:Number(f.casos_compartido.value),np_caso:Number(f.np_caso.value),pausar_con_isaac:f.pausar_con_isaac.checked});
 toast(r.msg||r.error,!r.ok);CFG_DIRTY=false;refreshStatus()};
async function refresh(){if(OFF)return;try{await Promise.all([refreshStatus(),refreshQueue(),refreshLog()])}catch(e){$('#wtext').textContent='Sin conexión con el panel';$('#wpill').className='pill off'}}
(async()=>{DEF=await get('/api/defaults');const f=$('#f1');for(const[k,v]of Object.entries(DEF.params))f[k].value=v;f.niter.value=DEF.niter;
 await refresh();await refreshResults();setInterval(refresh,5000);setInterval(()=>{if(!OFF)refreshResults()},30000)})();
</script></body></html>"""


def main():
    global SERVER
    args = sys.argv[1:]
    port = PORT
    if '--puerto' in args:
        port = int(args[args.index('--puerto') + 1])
    if '--oculto' in args:                         # sin ventana: la salida va a cola\panel.log
        f = open(os.path.join(QUEUE, 'panel.log'), 'a', encoding='utf-8', buffering=1)
        sys.stdout = sys.stderr = f
    print(f'---- {now()} panel en http://127.0.0.1:{port}', flush=True)
    for _ in range(10):
        try:
            SERVER = ThreadingHTTPServer(('127.0.0.1', port), H); break
        except OSError:
            time.sleep(0.5)
    else:
        print(f'El puerto {port} está ocupado: ¿ya hay un panel abierto? (el Go2 usa 8765, el B2 8770, Performance Lab 8090)', flush=True)
        sys.exit(1)
    print(f'WSL: {detect_distro()}', flush=True)
    threading.Thread(target=env_loop, daemon=True).start()
    threading.Thread(target=llm_status_loop, daemon=True).start()
    for be in ('ollama', 'anthropic'):
        threading.Thread(target=llm_loop, args=(be,), daemon=True).start()
    if '--arrancar-trabajador' in args:
        time.sleep(0.5)
        if not heartbeat()[1] and not os.path.exists(os.path.join(QUEUE, 'STOP')):
            start_worker(); log_panel('trabajador arrancado al abrir el panel')
    if '--navegador' in args:
        webbrowser.open(f'http://127.0.0.1:{port}')
    try:
        SERVER.serve_forever()
    except KeyboardInterrupt:
        pass
    print(f'---- {now()} panel cerrado', flush=True)


if __name__ == '__main__':
    main()
