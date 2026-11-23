#!/usr/bin/env python3
"""Trabajador de la cola de simulaciones del airbox (corre en WSL, oculto; lo arranca el panel de Windows).

Cola en C:\\ms_airbox\\cola (/mnt/c/ms_airbox/cola):
  pendientes/*.json   trabajos por hacer (por orden alfabético)
  en_curso/           el trabajo que se está calculando
  hechos/*.json       resultado: métricas, celdas, tiempos, núcleos usados
  fallidos/*.json     trabajos que fallaron, con el motivo
  cancelados/         trabajos cancelados desde el panel
  estado.json         latido cada 5 s: fase, iteración, núcleos, ROS 2 detectado...
  config.json         ajustes del panel: núcleos normal / compartido, pausar con Isaac Sim
  entorno.json        lo que ve el panel en Windows: Isaac Sim abierto, GPU, memoria
  STOP                si existe, termina al acabar el caso actual

Reproducibilidad: todos los casos se mallan y calculan con el MISMO número de procesos ('np_caso', 4 por
defecto), porque la división del dominio cambia ligeramente la malla y el resultado (~1-3 %). La carga del
PC solo cambia cuántos casos se calculan a la vez: 'casos_normal' sin otras simulaciones abiertas,
'casos_compartido' con Isaac Sim (Windows) o ROS 2 (WSL) en marcha. Prioridad mínima (nice 19) siempre.
Con 'pausar_con_isaac' no se empiezan casos nuevos mientras Isaac Sim esté abierto.
"""
import glob, json, os, platform, re, shutil, socket, subprocess, sys, threading, time, traceback

ROOT = os.environ.get('AIRBOX_ROOT', os.path.dirname(os.path.abspath(__file__)))
QUEUE = os.environ.get('AIRBOX_QUEUE', '/mnt/c/ms_airbox/cola')
CPUS = os.cpu_count() or 2
VMIN = 2.5   # L, volumen mínimo de plenum (2,4 L de reglamento + margen)
POLL = 15    # s entre comprobaciones con la cola vacía o en pausa

os.chdir(ROOT); sys.path.insert(0, ROOT)
for d in ('pendientes', 'en_curso', 'hechos', 'fallidos', 'cancelados'):
    os.makedirs(os.path.join(QUEUE, d), exist_ok=True)
os.makedirs(os.path.join(ROOT, 'geoms'), exist_ok=True); os.makedirs(os.path.join(ROOT, 'cases'), exist_ok=True)

STATE = {'nota': None}
RUNNING = {}            # fichero -> {'name', 'desde', 'niter', 'np', 'hilo'}
METRICS_LOCK = threading.Lock()


def now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def rjson(path, default=None):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def wjson(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    for _ in range(10):                       # Windows puede tener el fichero abierto un instante
        try:
            os.replace(tmp, path); return
        except OSError:
            time.sleep(0.2)


def log(msg):
    line = f'{now()}  {msg}'
    with open(os.path.join(QUEUE, 'lanzador.log'), 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def ros2_running():
    """Procesos de ROS 2 o Zenoh en esta distribución de WSL (ROS 2 Jazzy de los proyectos Go2/B2)."""
    try:
        r = subprocess.run(['pgrep', '-af', r'/opt/ros/|zenohd|rmw_zenohd|ros2 (launch|run)'],
                           capture_output=True, text=True, timeout=5)
        return [l for l in r.stdout.splitlines() if 'pgrep' not in l]
    except Exception:
        return []


def config():
    c = rjson(os.path.join(QUEUE, 'config.json'), {}) or {}
    np_caso = int(c.get('np_caso') or 4)
    cores = max(1, CPUS // 2)                    # núcleos físicos aproximados (los hilos SMT no aceleran la CFD)
    return {'np_caso': max(1, min(np_caso, CPUS)),
            'casos_normal': int(c.get('casos_normal') or max(1, cores // np_caso)),
            'casos_compartido': int(c.get('casos_compartido') or 1),
            'pausar_con_isaac': bool(c.get('pausar_con_isaac', False))}


_ENV_SEEN = {'mtime': None, 'mono': 0.0}


def entorno():
    """Lo que el panel de Windows ha visto (Isaac Sim abierto...). Se ignora si el panel lleva más de 60 s
    sin actualizarlo (cerrado). Se mide con el reloj monotónico de WSL, no con la hora, que puede desfasarse."""
    path = os.path.join(QUEUE, 'entorno.json')
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {}
    if mt != _ENV_SEEN['mtime']:
        _ENV_SEEN.update(mtime=mt, mono=time.monotonic())
    if time.monotonic() - _ENV_SEEN['mono'] > 60:
        return {}
    return rjson(path, {}) or {}


def case_progress(name, niter):
    case = os.path.join(ROOT, 'cases', name)
    out = {'fase': 'geometría', 'iter': 0}
    lf = os.path.join(case, 'log.simpleFoam')
    if os.path.exists(lf):
        out['fase'] = 'cálculo'
        try:
            with open(lf, 'rb') as f:
                f.seek(0, 2); n = f.tell(); f.seek(max(0, n - 20000))
                its = re.findall(r'^Time = (\d+)', f.read().decode(errors='ignore'), re.M)
            if its:
                out['iter'] = int(its[-1])
        except Exception:
            pass
        if niter and out['iter'] >= niter:
            out['fase'] = 'post-proceso'
    elif os.path.exists(os.path.join(case, 'log.blockMesh')):
        out['fase'] = 'mallado'
    return out


def case_log_tail(name, n=12):
    """Últimas líneas del registro más reciente del caso (para diagnosticar desde Windows)."""
    try:
        logs = glob.glob(os.path.join(ROOT, 'cases', name, 'log.*'))
        if not logs:
            return ''
        f = max(logs, key=os.path.getmtime)
        with open(f, 'rb') as fh:
            fh.seek(0, 2); size = fh.tell(); fh.seek(max(0, size - 4000))
            lines = fh.read().decode(errors='ignore').splitlines()[-n:]
        return os.path.basename(f) + ' (' + time.strftime('%H:%M:%S', time.localtime(os.path.getmtime(f))) + '):\n' + '\n'.join(lines)
    except Exception as e:
        return f'error leyendo el registro: {e}'


def heartbeat_loop():
    while True:
        try:
            trab = []
            for fn, r in list(RUNNING.items()):
                t = {'archivo': fn, 'name': r['name'], 'desde': r['desde'], 'niter': r['niter'], 'np': r['np']}
                t.update(case_progress(r['name'], r['niter'])); t['log_caso'] = case_log_tail(r['name'])
                trab.append(t)
            st = dict(host=socket.gethostname(), pid=os.getpid(), pgid=os.getpgid(0), cpus=CPUS,
                      python=platform.python_version(), actualizado=now(), t=time.time(),
                      pendientes=len(glob.glob(os.path.join(QUEUE, 'pendientes', '*.json'))),
                      ros2=len(ros2_running()) > 0, mpi=os.environ.get('AIRBOX_MPI', '1') == '1',
                      trabajos=trab, nota=STATE['nota'])
            if trab:                                   # compatibilidad con paneles antiguos: el primero
                st.update(trabajo=trab[0]['name'], desde=trab[0]['desde'], niter=trab[0]['niter'], np=trab[0]['np'],
                          fase=trab[0]['fase'], iter=trab[0]['iter'], log_caso=trab[0]['log_caso'])
            try:
                with open('/proc/meminfo') as f:
                    mi = dict(l.split(':', 1) for l in f)
                st['wsl_mem_total_gb'] = round(int(mi['MemTotal'].split()[0]) / 1048576, 1)
                st['wsl_mem_libre_gb'] = round(int(mi['MemAvailable'].split()[0]) / 1048576, 1)
            except Exception:
                pass
            wjson(os.path.join(QUEUE, 'estado.json'), st)
        except Exception:
            pass
        time.sleep(5)


def plenum_volume(stl):
    import trimesh, geom
    m = trimesh.load(stl, force='mesh'); m.merge_vertices(); trimesh.repair.fix_normals(m); m.apply_scale(1000)
    a = m.slice_plane([-205, 0, 0], [1, 0, 0], cap=True)
    b = a.slice_plane([0, geom.FLOOR - 0.05, 0], [0, -1, 0], cap=True)
    return (a.volume - b.volume) / 1e6


def mesh_quality(case):
    """Calidad de la malla según checkMesh (para filtrar o señalar casos en el análisis)."""
    q = {}
    try:
        s = open(os.path.join(case, 'log.checkMesh')).read()
        m = re.search(r'non-orthogonality Max:\s*([\d.]+)', s); q['no_ortog_max'] = float(m.group(1)) if m else None
        m = re.search(r'Max skewness = ([\d.]+)', s); q['asimetria_max'] = float(m.group(1)) if m else None
        m = re.search(r'Failed (\d+) mesh checks', s); q['checks_fallidos'] = int(m.group(1)) if m else 0
        q['mesh_ok'] = 'Mesh OK' in s
    except Exception:
        pass
    return q


def cells(case):
    try:
        s = open(os.path.join(case, 'log.checkMesh')).read()
        return int(re.search(r'cells:\s+(\d+)', s).group(1))
    except Exception:
        return None


def plan():
    """(procesos por caso, casos a la vez, texto del modo, pausa)."""
    c, e = config(), entorno()
    if os.environ.get('AIRBOX_MPI') == '0':
        np_, why_np = 1, ' · en serie: MPI no funciona (cola/mpi_test.log)'
    else:
        np_, why_np = c['np_caso'], ''
    ros = ros2_running(); isaac = e.get('isaac') or []
    shared = bool(ros or isaac)
    why = ', '.join(x for x in (('Isaac Sim' if isaac else ''), ('ROS 2' if ros else '')) if x)
    slots = c['casos_compartido'] if shared else c['casos_normal']
    slots = max(1, min(slots, max(1, CPUS // np_)))
    pause = bool(c['pausar_con_isaac'] and isaac)
    modo = (f'modo compartido ({why})' if shared else 'modo normal') + f' · {slots} a la vez × {np_} procesos' + why_np
    return np_, slots, modo, pause


T_GEOM = 600        # s: geometría (normalmente 20-60 s)
T_CFD = 4 * 3600    # s: malla + cálculo


def run_limited(cmd, timeout):
    """subprocess.run con límite de tiempo que, si se agota, mata también los nietos (mpirun, OpenFOAM)."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, 9)
        except OSError:
            pass
        p.communicate()
        raise RuntimeError(f'tiempo agotado ({timeout // 60} min) en {os.path.basename(cmd[1])}')
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def run_job(job, NP):
    name = job['name']; p = dict(job['params'], name=name)
    h = job.get('h', 0.007); niter = job.get('niter', 1000)
    res = dict(job=job, inicio=now(), host=socket.gethostname(), np=NP)
    t0 = time.time()
    shutil.rmtree(os.path.join(ROOT, 'cases', name), ignore_errors=True)   # restos de un intento interrumpido
    stl = os.path.join(ROOT, 'geoms', f'{name}.stl')
    r = run_limited([sys.executable, 'geom.py', json.dumps(p), stl, stl.replace('.stl', '.json')], T_GEOM)
    if r.returncode:
        raise RuntimeError('geometría: ' + r.stderr[-800:])
    res['t_geom_s'] = round(time.time() - t0, 1)
    res['geom_info'] = rjson(stl.replace('.stl', '.json'))
    v = plenum_volume(stl); res['vol_plenum_L'] = round(v, 3)
    if v < VMIN:
        if not job.get('ignorar_volumen'):
            res['status'] = 'infeasible'; res['nota'] = f'volumen de plenum {v:.2f} L < {VMIN} L'
            return res
        res['nota'] = f'referencia: volumen {v:.2f} L < {VMIN} L (calculado igualmente)'
    t1 = time.time()
    r = run_limited([sys.executable, 'runcase.py', name, stl, '--h', str(h), '--niter', str(niter), '--np', str(NP)], T_CFD)
    case = os.path.join(ROOT, 'cases', name)
    res['celdas'] = cells(case)
    res['malla'] = mesh_quality(case)
    if r.returncode:
        raise RuntimeError('runcase: ' + (r.stdout[-600:] + r.stderr[-600:]))
    res['t_cfd_s'] = round(time.time() - t1, 1)
    with METRICS_LOCK:
        import metrics
        m = metrics.case_metrics(name, p['R'], p['D'], niter // 2)
    res['metrics'] = {k: (float(x) if isinstance(x, (int, float)) or hasattr(x, 'item') else x) for k, x in m.items()}
    res['status'] = 'done'; res['t_total_s'] = round(time.time() - t0, 1); res['fin'] = now()
    if not job.get('keep_case', False):
        shutil.rmtree(case, ignore_errors=True)
        if os.path.exists(stl):
            os.remove(stl)       # se conserva el STEP (pequeño) para el conjunto de datos
    return res


def job_thread(fn, job, NP, modo):
    cur = os.path.join(QUEUE, 'en_curso', fn)
    try:
        res = run_job(job, NP)
        res['modo'] = modo
        wjson(os.path.join(QUEUE, 'hechos', fn), res)
        mm = res.get('metrics', {})
        log(f'{job["name"]}: {res["status"]}  dp0_ext={mm.get("dp0_ext")} Pa  dp0_O={mm.get("dp0_O")} Pa  '
            f'celdas={res.get("celdas")}  t={res.get("t_total_s")} s')
        if res.get('status') == 'done':
            try:
                vista_request(job['name'])
            except Exception:
                pass
    except Exception as e:
        err = dict(job=job, status='failed', error=str(e)[-1500:], trace=traceback.format_exc()[-1500:], fin=now())
        wjson(os.path.join(QUEUE, 'fallidos', fn), err)
        log(f'{job.get("name")}: FALLO  {str(e)[-200:]}')
    finally:
        try:
            os.remove(cur)
        except OSError:
            pass
        RUNNING.pop(fn, None)


VISTA_RE = __import__('re').compile(r'^[A-Za-z0-9_\-]{1,60}$')


def vista_request(name):
    d = os.path.join(QUEUE, 'vista', 'peticiones'); os.makedirs(d, exist_ok=True)
    open(os.path.join(d, name + '.req'), 'w').close()


def vista_loop():
    """Vistas 3D para el visor del panel (vista.py): convierte el STEP del caso pedido en un STL ligero."""
    d = os.path.join(QUEUE, 'vista', 'peticiones'); os.makedirs(d, exist_ok=True)
    while True:
        try:
            names = []
            for f in glob.glob(os.path.join(d, '*.req'))[:20]:
                n = os.path.basename(f)[:-4]
                try:
                    os.remove(f)
                except OSError:
                    continue
                if VISTA_RE.match(n):
                    names.append(n)
            if names:
                run_limited([sys.executable, 'vista.py'] + names, 300)
        except Exception as e:
            log(f'vista: {e}')
        time.sleep(3)


def main():
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=vista_loop, daemon=True).start()
    try:                                       # estudios de optimización (estudios/comparacion.json y ampliado.json)
        import estudio
        estudio.start_all()
    except Exception as e:
        log(f'estudio no disponible: {e}')
    log(f'Trabajador iniciado en {socket.gethostname()} ({CPUS} hilos en WSL, prioridad nice {os.nice(0)}). Cola: {QUEUE}')
    last_note = None
    while True:
        stopping = os.path.exists(os.path.join(QUEUE, 'STOP'))
        if stopping and not RUNNING:
            log('STOP encontrado: termino.'); STATE['nota'] = 'detenido'; time.sleep(6); break
        np_, slots, modo, pause = plan()
        # casos que quedaron en en_curso de un arranque anterior se repiten primero
        leftovers = [p for p in sorted(glob.glob(os.path.join(QUEUE, 'en_curso', '*.json'))) if os.path.basename(p) not in RUNNING]
        pending = sorted(glob.glob(os.path.join(QUEUE, 'pendientes', '*.json')))
        cands = leftovers + ([] if (pause or stopping) else pending)
        if stopping:
            note = 'parando: termina los casos en curso'
        elif pause and not RUNNING and not leftovers:
            note = 'en pausa: Isaac Sim abierto'
        elif not cands and not RUNNING:
            note = 'cola vacía'
        else:
            note = modo
        if note != last_note and note in ('cola vacía', 'en pausa: Isaac Sim abierto'):
            log('Cola vacía: espero trabajos nuevos.' if note == 'cola vacía' else 'En pausa: Isaac Sim está abierto.')
        STATE['nota'] = note; last_note = note
        launched = False
        while len(RUNNING) < slots and cands:
            src = cands.pop(0); fn = os.path.basename(src)
            cur = os.path.join(QUEUE, 'en_curso', fn)
            if src != cur:
                try:
                    shutil.move(src, cur)
                except (FileNotFoundError, PermissionError):      # el panel lo acaba de mover o cancelar
                    continue
            job = rjson(cur)
            if not job:
                log(f'{fn}: JSON ilegible'); shutil.move(cur, os.path.join(QUEUE, 'fallidos', fn)); continue
            RUNNING[fn] = {'name': job.get('name'), 'desde': now(), 'niter': job.get('niter', 1000), 'np': np_}
            log(f'{job.get("name")}: empiezo (h={job.get("h", 0.007)}, iter={job.get("niter", 1000)}, {modo})')
            th = threading.Thread(target=job_thread, args=(fn, job, np_, modo), daemon=True)
            RUNNING[fn]['hilo'] = th; th.start(); launched = True
            time.sleep(2)
        time.sleep(3 if (launched or RUNNING) else POLL)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log('Interrumpido (los casos en curso se repetirán al relanzar).')
