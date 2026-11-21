from contextlib import asynccontextmanager
from importlib import import_module
from fastapi import FastAPI, Request, Body
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from app.config import ROOT, META, MODULES, DB_PATH
from app.errors import BusinessError
from app.engine import evaluate, merge_instructions
from app import store
from app.archive import serialize

@asynccontextmanager
async def lifespan(app):
    store.ensure(DB_PATH,evaluate())
    yield
app=FastAPI(title=META['title'],lifespan=lifespan)
app.mount('/static',StaticFiles(directory=str(ROOT/'app/static')),name='static')
@app.exception_handler(BusinessError)
async def business_error(request:Request,error:BusinessError):
    return JSONResponse({'code':error.code,'detail':error.message},status_code=error.status)
@app.get('/')
def home():
    return RedirectResponse('/'+MODULES[0],status_code=303)
@app.get('/healthz')
def health():
    return {'status':'ok','project':META['short']}
@app.post('/api/runs')
def create(payload:dict=Body(default={})):
    return store.save(DB_PATH,evaluate(payload))
@app.post('/api/runs/{run_id}/rerun')
def rerun(run_id:str):
    previous=store.get(DB_PATH,run_id)
    if 'input_snapshot' not in previous:
        raise BusinessError('INPUT_SNAPSHOT_MISSING','旧批缺少源快照，请重新导入')
    return store.save(DB_PATH,evaluate({'dataset':previous['source']},snapshot=previous['input_snapshot']))
@app.post('/api/runs/{run_id}/switch')
def switch(run_id:str,payload:dict=Body(default={})):
    """切换箱门口径。整批先重算、校验不过即 422；校验通过才在同一事务落库。

    指令是整批口径的全集：本次提交的箱覆盖同名箱，其余箱沿用已有决定。
    同一 instruction 重试不新增版本；任何异常都不会写入 half-state，
    旧版结果和输入快照里的原始封签保持可读。
    """
    previous=store.get(DB_PATH,run_id)
    if 'input_snapshot' not in previous:
        raise BusinessError('INPUT_SNAPSHOT_MISSING','旧批缺少源快照，请重新导入')
    revision=payload.get('revision')
    if revision is not None and (not isinstance(revision,int) or revision<1):
        raise BusinessError('BAD_REVISION','版本号须为正整数')
    merged=merge_instructions(previous['input_snapshot'].get('handover') or {},payload.get('handover') or {})
    attempted=evaluate({'dataset':previous['source'],'handover':merged},snapshot=previous['input_snapshot'])
    instructions=attempted['input_snapshot'].get('handover',{})
    return store.switch(DB_PATH,run_id,instructions,attempted,revision)
@app.get('/api/runs/{run_id}/versions')
def versions(run_id:str):
    return store.versions(DB_PATH,run_id)
@app.post('/api/runs/{run_id}/restore')
def restore(run_id:str,payload:dict=Body(...)):
    version_no=payload.get('version_no')
    if not isinstance(version_no,int) or version_no<1: raise BusinessError('BAD_REVISION','版本号须为正整数')
    return store.restore(DB_PATH,run_id,version_no)
@app.get('/api/runs')
def runs():
    return store.history(DB_PATH)
@app.get('/api/runs/{run_id}')
def run(run_id:str):
    return store.get(DB_PATH,run_id)
@app.post('/api/runs/{run_id}/review')
def review(run_id:str,payload:dict=Body(...)):
    revision=payload.get('revision')
    if not isinstance(revision,int) or revision<1: raise BusinessError('BAD_REVISION','版本号须为正整数')
    return store.review(DB_PATH,run_id,revision)
@app.get('/api/export/{run_id}')
def export(run_id:str):
    result=store.get(DB_PATH,run_id)
    return Response(serialize(result),media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="{run_id}.zip"'})
for key in MODULES:
    app.include_router(import_module(f'app.modules.{key}.handler').router)
