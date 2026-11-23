#!/usr/bin/env python3
"""Prepara, malla y resuelve un caso OpenFOAM del airbox. Uso: runcase.py nombre stl [--h 0.005] [--lsurf 1] [--lrun 2] [--nlay 3] [--niter 1500] [--mdot 0.0336] [--v 0] [--only-mesh]"""
import math, argparse,os,re,shutil,subprocess,sys
ap=argparse.ArgumentParser()
ap.add_argument('name'); ap.add_argument('stl')
ap.add_argument('--h',type=float,default=0.005); ap.add_argument('--lsurf',type=int,default=1); ap.add_argument('--lrun',type=int,default=2)
ap.add_argument('--lfeat',type=int,default=1); ap.add_argument('--nlay',type=int,default=3); ap.add_argument('--niter',type=int,default=1500)
ap.add_argument('--mdot',type=float,default=0.0336); ap.add_argument('--v',type=float,default=0.0,help='km/h')
ap.add_argument('--np',type=int,default=int(os.environ.get('AIRBOX_NP','2'))); ap.add_argument('--resume',action='store_true'); ap.add_argument('--only-mesh',action='store_true'); ap.add_argument('--skip-mesh',action='store_true')
a=ap.parse_args()
RHO=1.153
root=os.environ.get('AIRBOX_ROOT',os.path.dirname(os.path.abspath(__file__))); case=os.path.join(root,'cases',a.name)
def sh(cmd,log):
    with open(os.path.join(case,log),'w') as f:
        r=subprocess.run(cmd,shell=True,cwd=case,stdout=f,stderr=subprocess.STDOUT,env=dict(os.environ,WM_PROJECT_DIR=os.environ.get('WM_PROJECT_DIR','/usr/share/openfoam'),FOAM_ETC=os.environ.get('FOAM_ETC','/usr/share/openfoam/etc'),OMPI_ALLOW_RUN_AS_ROOT='1',OMPI_ALLOW_RUN_AS_ROOT_CONFIRM='1',OMPI_MCA_btl_vader_single_copy_mechanism='none'))
    if r.returncode: sys.exit(f'FALLO {cmd} (ver {log})')
def sub(path,rep):
    s=open(path).read()
    for k,v in rep.items(): s=s.replace(k,str(v))
    open(path,'w').write(s)
if not a.skip_mesh:
    if os.path.exists(case): shutil.rmtree(case)
    shutil.copytree(os.path.join(root,'template'),case)
    _b=max(d for d in range(1,int(a.np**0.5)+1) if a.np%d==0); _a=a.np//_b
    open(os.path.join(case,'system/decomposeParDict'),'w').write('FoamFile { version 2.0; format ascii; class dictionary; object decomposeParDict; }\n'+f'numberOfSubdomains {a.np}; method simple; simpleCoeffs {{ n ({_a} {_b} 1); delta 0.001; }}\n')
    os.makedirs(os.path.join(case,'constant/triSurface'),exist_ok=True)
    shutil.copy(a.stl,os.path.join(case,'constant/triSurface/geom.stl'))
    L=(0.82,0.27,0.19); BX=round(L[0]/a.h)
    # espacio ampliado: si la geometría se acerca al final del fondo (x = -0,025 m), se añaden celdas enteras del
    # mismo tamaño por detrás. Los casos que caben (todo el muestreo base) conservan exactamente la misma malla.
    import trimesh as _tm
    _xmax=float(_tm.load(a.stl,force='mesh').bounds[1][0]); _dx=L[0]/BX; _x1=-0.845+BX*_dx
    if _xmax>_x1-0.012:
        _k=int(math.ceil((_xmax+0.012-_x1)/_dx)); BX+=_k; _x1n=-0.845+BX*_dx
        _bm=f'{case}/system/blockMeshDict'; _s=open(_bm).read().replace('(-0.025 ',f'({_x1n:.6f} '); open(_bm,'w').write(_s)
        print(a.name,f'fondo ampliado {_k} celdas hasta x={_x1n:.4f} m',flush=True)
    sub(f'{case}/system/blockMeshDict',{'BX':BX,'BY':round(L[1]/a.h),'BZ':round(L[2]/a.h)})
    sub(f'{case}/system/snappyHexMeshDict',{'LSURF':a.lsurf,'LRUN':a.lrun,'LFEAT':a.lfeat,'NLAY':a.nlay})
    sub(f'{case}/system/controlDict',{'NITER':a.niter,'WINT':100})
    shutil.move(f'{case}/0',f'{case}/0.orig')
    sh('blockMesh','log.blockMesh'); sh('surfaceFeatureExtract','log.sfe')
    if a.np>1:
        sh('decomposePar -force',"log.decomp0")
        sh(f'mpirun -np {a.np} snappyHexMesh -overwrite -parallel','log.snappy')
        sh('reconstructParMesh -constant','log.recon0')
        for d in os.listdir(case):
            if d.startswith('processor'): shutil.rmtree(os.path.join(case,d))
    else:                                   # en serie: sin MPI
        sh('snappyHexMesh -overwrite','log.snappy')
    shutil.copytree(f'{case}/0.orig',f'{case}/0')
    # nombres de parches
    b=open(f'{case}/constant/polyMesh/boundary').read()
    names=re.findall(r'\n\s*(\w+)\s*\n\s*\{\s*\n\s*type',b)
    if re.search(r'bg\s*\{[^}]*nFaces\s+[1-9]', b): sys.exit('FUGA: la malla toca el fondo (bg)')
    names=[n for n in names if n!='bg']
    pin=[n for n in names if n.endswith('inlet')][0]; pout=[n for n in names if n.endswith('outlet')][0]; pw=[n for n in names if n.endswith('wall')][0]
    for f in os.listdir(f'{case}/0'):
        sub(f'{case}/0/{f}',{' inlet {':f' {pin} {{',' outlet {':f' {pout} {{',' wall {':f' {pw} {{'})
    sh('checkMesh','log.checkMesh')
    cm=open(f'{case}/log.checkMesh').read()
    cells=re.search(r'cells:\s+(\d+)',cm).group(1); print(a.name,'cells',cells, 'Mesh OK' if 'Mesh OK' in cm else 'MESH CHECKS FAILED', flush=True)
    if a.only_mesh: sys.exit()
q=0.5*RHO*(a.v/3.6)**2
sub(f'{case}/0/U',{'QVOL':f'{a.mdot/RHO:.6g}'}); sub(f'{case}/0/p',{'P0KIN':f'{q/RHO:.6g}'})
if a.resume and os.path.isdir(os.path.join(case,'processor0')):
    sh(f'foamDictionary system/controlDict -entry endTime -set {a.niter}','log.fd')
    with open(os.path.join(case,'log.simpleFoam'),'a') as f: f.write('\n#### RESUME ####\n')
    sh(f'mpirun -np {a.np} simpleFoam -parallel > log.simpleFoam.resume 2>&1; cat log.simpleFoam.resume >> log.simpleFoam','log.dummy')
    sh('reconstructPar -newTimes','log.recon')
    for d in os.listdir(case):
        if d.startswith('processor'): shutil.rmtree(os.path.join(case,d))
    print(a.name,'done',flush=True); sys.exit()
sh(f'foamDictionary system/controlDict -entry endTime -set {a.niter}','log.fd')
if a.np>1:
    sh('decomposePar -force','log.decomp')
    sh(f'mpirun -np {a.np} simpleFoam -parallel','log.simpleFoam')
    sh('reconstructPar','log.recon')
else:
    sh('simpleFoam','log.simpleFoam')
for d in os.listdir(case):
    if d.startswith('processor'): shutil.rmtree(os.path.join(case,d))
print(a.name,'done',flush=True)
