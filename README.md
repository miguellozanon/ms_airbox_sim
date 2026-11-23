# Airbox MS9 · panel de simulaciones

Mismo esquema que los launchers del Go2, del B2 y el Performance Lab de Formula Student:
un panel web local que corre en **Windows** con el Python de Isaac Sim (solo biblioteca estándar),
y el cálculo (OpenFOAM) en **WSL Ubuntu-24.04**, oculto y sin ventanas.

| Launcher | Puerto |
| --- | --- |
| Go2 | 8765 |
| B2 (PATROL) | 8770 |
| Performance Lab (FS) | 8090 |
| **Airbox MS9** | **8780** |

## Uso

| Para | Haz |
| --- | --- |
| Instalar (una vez) | `Instalar.bat`. Instala OpenFOAM y un entorno de Python propio en WSL (~15 min). |
| Abrir el panel | **Airbox MS9** (acceso directo en esta carpeta) o `Abrir_panel.bat`. Si ya está abierto, solo abre el navegador. |
| Cerrar el navegador | No pasa nada: el cálculo sigue. |
| Apagarlo todo | Botón **Apagar** del panel, o `Apagar_panel.bat`. |

## Convivencia con ROS 2 Jazzy e Isaac Sim

- **Puertos:** el panel usa el 8780 y no abre ningún otro. No usa DDS ni Zenoh (7447/7448).
- **GPU:** OpenFOAM no la usa; Isaac Sim la tiene entera.
- **CPU:** OpenFOAM corre con prioridad mínima (`nice 19`), así que ROS 2 en WSL va siempre antes.
  Isaac Sim corre en Windows, que no ve esa prioridad. Por eso el panel detecta Isaac Sim (y el trabajador, ROS 2)
  y calcula **menos casos a la vez**: por defecto 2 con el PC libre y 1 con Isaac Sim o ROS 2 abiertos.
- **Resultados comparables:** todos los casos se mallan y calculan con el **mismo número de procesos**
  (4 por defecto), porque repartir el dominio de otra forma cambia ligeramente la malla y la pérdida (~1–3 %).
  La carga del PC solo cambia cuántos casos van a la vez y lo que tardan, nunca el resultado.
  No cambies «Procesos por caso» en mitad de un estudio.
- **Experimentos sensibles al tiempo** (métricas de navegación, factor de tiempo real…): marca
  «No empezar casos nuevos mientras Isaac Sim esté abierto» o pulsa «Parar al acabar el caso».
- **Memoria:** un caso en malla gruesa usa ~1 GB; en malla fina, 3–4 GB. Comparte la memoria de WSL con ROS 2.
  El panel muestra la RAM libre de Windows y de WSL.
- **Software:** el entorno de Python está aislado (`~/airbox_env`) y no toca `/opt/ros` ni el Python de ROS 2.
- **Parar:** «Detener ya» y «Apagar» solo matan el árbol de procesos del airbox, nunca ROS 2 ni Isaac Sim.

## Carpetas

| Carpeta | Contenido |
| --- | --- |
| `app\` | Código: panel (Windows), trabajador, geometría (CadQuery), malla y cálculo (OpenFOAM), post-proceso |
| `cola\pendientes\`, `en_curso\`, `hechos\`, `fallidos\`, `cancelados\` | Los casos y sus resultados (JSON) |
| `cola\config.json` | Procesos normal / compartido, pausa con Isaac Sim y distribución de WSL detectada |
| `cola\lanzador.log`, `trabajador.log`, `panel.log` | Registros |

Los casos se calculan en el disco de Linux (`~/ms_airbox` en WSL), mucho más rápido que `C:`.
Cada vez que arranca el trabajador se copia el código de `app\` a WSL, así que Claude puede actualizarlo sin reinstalar.

## Primer caso: comprobación

Caso de referencia `D_ref` (variante D, malla gruesa, 4 procesos), calculado en este PC el 19/11/2026:
**58,3 Pa** con mezcla y **36,7 Pa** hasta la mariposa (295 661 celdas, 12,5 min con Isaac Sim y ROS 2 abiertos).
Con 2 procesos daba 56,8 y 36,2 Pa: es la diferencia que provoca cambiar el reparto, y por eso ahora es fijo.

## Si algo falla

- **No se abre el panel:** mira `cola\panel.log`.
- **El trabajador no arranca («Detenido»):** mira `cola\trabajador.log`. Si dice que falta la instalación, ejecuta `Instalar.bat`.
- **MPI:** al arrancar, el trabajador prueba `mpirun` y aplica la configuración que funcione (en este PC hay que
  desactivar las extensiones de GPU de `hwloc`). Si ninguna funciona, calcula en serie; detalle en `cola\mpi_test.log`.
- **Python:** se usa `C:\isaacsim\kit\python\python.exe`; si no existe, `C:\Python314\python.exe`.
- `app\arrancar_panel.sh` es de una versión anterior y se puede borrar.
