import asyncio
import csv
import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .core import connection, query, execute, event, git, DATA, ROOT
from .execution import status as execution_status
from .auth import (COOKIE, PUBLIC_USER, current_user, get_session, public_user,
                   password_hash, verify_password, start_session, require_project,
                   require_data_source, admin, audit, require_admin)
from .analytics import AGGREGATIONS, schema as data_schema, validate_metric

app = FastAPI(title='ESI · 企业超级智能平台', docs_url=None, redoc_url=None, openapi_url=None)
ORIGINS = [x.strip().rstrip('/') for x in os.environ.get(
    'ESI_PUBLIC_ORIGIN','http://localhost:5173,http://127.0.0.1:5173').split(',') if x.strip()]
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_credentials=True,
                   allow_methods=['GET','POST'], allow_headers=['Content-Type','X-ESI-CSRF','X-ESI-Client'])

@app.middleware('http')
async def browser_boundary(request: Request, call_next):
    if request.method not in ('GET','HEAD','OPTIONS'):
        if request.headers.get('X-ESI-Client') != 'workbench':
            return JSONResponse({'detail':'缺少客户端请求标识'},status_code=403)
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') not in ORIGINS:
            return JSONResponse({'detail':'请求来源不受信任'},status_code=403)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    return response

class Credentials(BaseModel):
    username: str = Field(min_length=1,max_length=80)
    password: str = Field(min_length=1,max_length=128)

class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1,max_length=128)
    new_password: str = Field(min_length=12,max_length=128)

class NewUser(BaseModel):
    username: str = Field(pattern=r'^[a-zA-Z0-9_.-]{3,64}$')
    display_name: str = Field(min_length=1,max_length=80)
    password: str = Field(min_length=12,max_length=128)
    role: Literal['admin','member'] = 'member'

class UserUpdate(BaseModel):
    role: Literal['admin','member']
    active: bool

class ResetPassword(BaseModel):
    password: str = Field(min_length=12,max_length=128)

class Member(BaseModel):
    user_id: str
    role: Literal['maintainer','developer','viewer','remove']

class Project(BaseModel):
    name: str = Field(min_length=1,max_length=100)
    path: str
    branch: str = 'HEAD'
    checks: list[str] = Field(min_length=1)

class Task(BaseModel):
    project_id: str
    title: str = Field(min_length=1,max_length=200)
    requirement: str = Field(min_length=1,max_length=20000)
    acceptance: str = Field(min_length=1,max_length=10000)

class Plan(BaseModel):
    plan: str = Field(min_length=1,max_length=30000)
    version: int

class Approval(BaseModel):
    version: int

class DataSourceInput(BaseModel):
    name: str = Field(min_length=1,max_length=100)
    kind: Literal['sqlite','csv']
    path: str
    description: str = Field(default='',max_length=500)

class DataMember(BaseModel):
    user_id: str
    role: Literal['owner','analyst','viewer','remove']

class MetricInput(BaseModel):
    name: str = Field(min_length=1,max_length=100)
    description: str = Field(default='',max_length=500)
    table_name: str = Field(min_length=1,max_length=128)
    aggregation: Literal['sum','avg','count','count_distinct','min','max']
    value_column: Optional[str] = Field(default=None,max_length=128)
    date_column: Optional[str] = Field(default=None,max_length=128)
    dimensions: list[str] = Field(default_factory=list,max_length=8)

class AnalysisInput(BaseModel):
    source_id: str
    title: str = Field(min_length=1,max_length=200)
    question: str = Field(min_length=1,max_length=20000)
    report_kind: Literal['report','strategy'] = 'report'

class AnalysisPlan(BaseModel):
    plan: str = Field(min_length=1,max_length=30000)
    version: int

# Dummy work keeps invalid usernames from skipping the password hash cost.
DUMMY_HASH = password_hash('nonexistent-account-placeholder')

@app.get('/api/health')
def health():
    return {'ok':True}

@app.post('/api/auth/login')
def login(data: Credentials, request: Request, response: Response):
    username = data.username.lower().strip()
    ip = request.client.host if request.client else 'unknown'
    buckets = ['account:'+hashlib.sha256(username.encode()).hexdigest(), 'ip:'+ip]
    now=time.time()
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('DELETE FROM login_attempts WHERE created<?',(now-900,))
        for bucket,limit in zip(buckets,[10,300]):
            count=c.execute('SELECT COUNT(*) FROM login_attempts WHERE bucket=?',(bucket,)).fetchone()[0]
            if count>=limit:
                raise HTTPException(429,'登录尝试过多，请 15 分钟后重试')
        c.executemany('INSERT INTO login_attempts VALUES(?,?)',[(b,now) for b in buckets])
    with connection() as c:
        row=c.execute('SELECT * FROM users WHERE username=?',(username,)).fetchone()
        valid=verify_password(data.password,row['password_hash'] if row else DUMMY_HASH)
        if not row or not valid or not row['active']:
            audit(c,None,'login_failed',username)
        else:
            # Revoke the previous cookie when switching accounts in this browser.
            old=request.cookies.get(COOKIE,'')
            c.execute('DELETE FROM sessions WHERE token_hash=? OR expires<?',
                      (hashlib.sha256(old.encode()).hexdigest(),now))
            csrf=start_session(c,response,row['id'])
            c.execute('DELETE FROM login_attempts WHERE bucket=?',(buckets[0],))
            audit(c,row['id'],'login',row['id'])
            return {'user':public_user(dict(row)), 'csrf':csrf}
    raise HTTPException(401,'用户名或密码错误，或账号已停用')

@app.get('/api/auth/me')
def me(request: Request):
    user=get_session(request)
    return {'user':public_user(user),'csrf':user['csrf']}

@app.post('/api/auth/logout')
def logout(request: Request, response: Response):
    user=get_session(request)
    if not hmac.compare_digest(request.headers.get('X-ESI-CSRF',''),user['csrf']):
        raise HTTPException(403,'请求校验失败')
    with connection() as c:
        c.execute('DELETE FROM sessions WHERE token_hash=?',(user['token_hash'],))
        audit(c,user['id'],'logout',user['id'])
    response.delete_cookie(COOKIE,path='/')
    return {'ok':True}

@app.post('/api/auth/password')
def change_password(data: PasswordChange, request: Request, response: Response):
    user=get_session(request)
    if not hmac.compare_digest(request.headers.get('X-ESI-CSRF',''),user['csrf']):
        raise HTTPException(403,'请求校验失败')
    if not verify_password(data.current_password,user['password_hash']):
        raise HTTPException(400,'当前密码错误')
    if data.current_password==data.new_password:
        raise HTTPException(400,'新密码不能与当前密码相同')
    new_hash=password_hash(data.new_password)
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        updated=c.execute('UPDATE users SET password_hash=?,must_change_password=0 WHERE id=? AND password_hash=? AND active=1',
                          (new_hash,user['id'],user['password_hash']))
        if updated.rowcount!=1:
            raise HTTPException(409,'账号状态已变化，请重新登录')
        c.execute('DELETE FROM sessions WHERE user_id=?',(user['id'],))
        csrf=start_session(c,response,user['id'])
        audit(c,user['id'],'password_changed',user['id'])
    if user['username']=='admin':
        (DATA/'bootstrap-admin.txt').unlink(missing_ok=True)
    user['must_change_password']=0
    return {'user':public_user(user),'csrf':csrf}

@app.get('/api/users')
def users(user=Depends(current_user)):
    admin(user)
    return query('SELECT '+PUBLIC_USER+' FROM users ORDER BY created')

@app.get('/api/directory')
def directory(user=Depends(current_user)):
    # Internal directory intentionally contains no credentials, sessions or account role.
    return query('SELECT id,username,display_name FROM users WHERE active=1 ORDER BY display_name')

@app.post('/api/users')
def create_user(data: NewUser, user=Depends(current_user)):
    admin(user)
    uid=uuid.uuid4().hex[:12]
    try:
        with connection() as c:
            c.execute('BEGIN IMMEDIATE')
            require_admin(c,user)
            c.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?,?)',
                      (uid,data.username.lower(),data.display_name,password_hash(data.password),data.role,1,1,time.time()))
            audit(c,user['id'],'user_created',uid,data.role)
    except sqlite3.IntegrityError:
        raise HTTPException(409,'用户名已存在')
    return {'id':uid}

@app.post('/api/users/{uid}')
def update_user(uid: str, data: UserUpdate, user=Depends(current_user)):
    admin(user)
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        require_admin(c,user)
        target=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
        if not target:
            raise HTTPException(404,'用户不存在')
        if target['role']=='admin' and target['active'] and (data.role!='admin' or not data.active):
            count=c.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]
            if count<=1:
                raise HTTPException(409,'不能停用或降级最后一位管理员')
        c.execute('UPDATE users SET role=?,active=? WHERE id=?',(data.role,int(data.active),uid))
        c.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
        audit(c,user['id'],'user_updated',uid,json.dumps(data.model_dump()))
    return {'ok':True}

@app.post('/api/users/{uid}/password')
def reset_password(uid: str, data: ResetPassword, user=Depends(current_user)):
    admin(user)
    if uid==user['id']:
        raise HTTPException(400,'请使用个人修改密码功能')
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        require_admin(c,user)
        if not c.execute('SELECT id FROM users WHERE id=?',(uid,)).fetchone():
            raise HTTPException(404,'用户不存在')
        c.execute('UPDATE users SET password_hash=?,must_change_password=1 WHERE id=?',(password_hash(data.password),uid))
        c.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
        audit(c,user['id'],'password_reset',uid)
    return {'ok':True}

@app.get('/api/audit')
def audit_log(user=Depends(current_user)):
    admin(user)
    return query('SELECT a.*,u.display_name AS actor_name FROM audit a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT 300')

@app.get('/api/settings')
def settings(user=Depends(current_user)):
    return {'configured':bool(os.environ.get('MODEL_API_KEY') and os.environ.get('MODEL_NAME')),
            'base_url':os.environ.get('MODEL_BASE_URL','https://api.openai.com/v1') if user['role']=='admin' else '由管理员配置',
            'model':os.environ.get('MODEL_NAME',''),'mode':'企业团队 · 项目权限隔离','execution':execution_status()}

@app.get('/api/data-sources')
def data_sources(user=Depends(current_user)):
    if user['role']=='admin':
        rows=query("SELECT d.*,'owner' AS my_role FROM data_sources d ORDER BY d.created")
    else:
        rows=query('SELECT d.*,m.role AS my_role FROM data_sources d JOIN data_source_members m ON m.source_id=d.id WHERE m.user_id=? ORDER BY d.created',(user['id'],))
    result=[]
    with connection() as c:
        for row in rows:
            item=dict(row)
            item['metrics']=[dict(metric) for metric in c.execute('SELECT * FROM metrics WHERE source_id=? ORDER BY created',(item['id'],))]
            for metric in item['metrics']:
                metric['dimensions']=json.loads(metric['dimensions'])
            # Server file paths are restricted to owners.
            if item['my_role']!='owner': item['path']='由数据负责人管理'
            result.append(item)
    return result

@app.post('/api/data-sources')
def create_data_source(data: DataSourceInput, user=Depends(current_user)):
    admin(user)
    sid=uuid.uuid4().hex[:12]
    try:
        available=data_schema(data.kind,data.path)
    except Exception as exc:
        raise HTTPException(400,'数据源配置无效：'+str(exc))
    with connection() as c:
        c.execute('BEGIN IMMEDIATE');require_admin(c,user)
        c.execute('INSERT INTO data_sources VALUES(?,?,?,?,?,?,?)',
                  (sid,data.name,data.kind,str(Path(data.path).expanduser().resolve()),data.description,user['id'],time.time()))
        c.execute('INSERT INTO data_source_members VALUES(?,?,?)',(sid,user['id'],'owner'))
        audit(c,user['id'],'data_source_created',sid,data.name)
    return {'id':sid,'schema':available}

@app.get('/api/data-sources/{sid}/schema')
def source_schema(sid: str, user=Depends(current_user)):
    with connection() as c: source=require_data_source(c,user,sid,'analyst')
    try: return data_schema(source['kind'],source['path'])
    except Exception as exc: raise HTTPException(400,'无法读取数据结构：'+str(exc))

@app.get('/api/data-sources/{sid}/members')
def data_members(sid: str, user=Depends(current_user)):
    with connection() as c:
        require_data_source(c,user,sid,'owner')
        return [dict(row) for row in c.execute('''SELECT m.*,u.username,u.display_name,u.active
          FROM data_source_members m JOIN users u ON u.id=m.user_id WHERE m.source_id=?''',(sid,))]

@app.post('/api/data-sources/{sid}/members')
def set_data_member(sid: str, data: DataMember, user=Depends(current_user)):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE');require_data_source(c,user,sid,'owner')
        target=c.execute('SELECT id FROM users WHERE id=? AND active=1',(data.user_id,)).fetchone()
        if not target and data.role!='remove': raise HTTPException(400,'用户不存在或已停用')
        if data.user_id==user['id'] and user['role']!='admin' and data.role!='owner':
            raise HTTPException(409,'数据负责人不能移除或降级自己，请联系管理员')
        if data.role=='remove':
            c.execute('DELETE FROM data_source_members WHERE source_id=? AND user_id=?',(sid,data.user_id))
        else:
            c.execute('INSERT INTO data_source_members VALUES(?,?,?) ON CONFLICT(source_id,user_id) DO UPDATE SET role=excluded.role',(sid,data.user_id,data.role))
        audit(c,user['id'],'data_membership_updated',sid,data.user_id+':'+data.role)
    return {'ok':True}

@app.post('/api/data-sources/{sid}/metrics')
def create_metric(sid: str, data: MetricInput, user=Depends(current_user)):
    mid=uuid.uuid4().hex[:12]
    with connection() as c:
        c.execute('BEGIN IMMEDIATE');source=require_data_source(c,user,sid,'owner')
        metric={**data.model_dump(),'id':mid,'source_id':sid}
        try: validate_metric(source,metric)
        except Exception as exc: raise HTTPException(400,'指标配置无效：'+str(exc))
        c.execute('INSERT INTO metrics VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                  (mid,sid,data.name,data.description,data.table_name,data.aggregation,
                   data.value_column,data.date_column,json.dumps(data.dimensions,ensure_ascii=False),user['id'],time.time()))
        audit(c,user['id'],'metric_created',mid,data.name)
    return {'id':mid}

def analysis_access(c,jid,user,permission='read'):
    job=c.execute('SELECT * FROM analysis_jobs WHERE id=?',(jid,)).fetchone()
    if not job: raise HTTPException(404,'分析任务不存在或无权访问')
    source=require_data_source(c,user,job['source_id'],'owner' if permission=='review' else 'viewer')
    if permission=='edit' and source['my_role']!='owner':
        if source['my_role']!='analyst' or job['created_by']!=user['id']:
            raise HTTPException(403,'只能修改或取消自己创建的分析任务')
    return dict(job),source

def decorate_analysis(c,job,user,source):
    job['can_edit']=source['my_role']=='owner' or (source['my_role']=='analyst' and job['created_by']==user['id'])
    job['can_review']=source['my_role']=='owner'
    for field in ('created_by','approved_by','completed_by'):
        actor=c.execute('SELECT display_name FROM users WHERE id=?',(job[field],)).fetchone()
        job[field+'_name']=actor['display_name'] if actor else '尚未指定'
    return job

@app.get('/api/analysis-jobs')
def analysis_jobs(user=Depends(current_user)):
    if user['role']=='admin':
        return query('SELECT * FROM analysis_jobs ORDER BY created DESC')
    return query('''SELECT j.* FROM analysis_jobs j JOIN data_source_members m ON m.source_id=j.source_id
      WHERE m.user_id=? ORDER BY j.created DESC''',(user['id'],))

@app.post('/api/analysis-jobs')
def create_analysis_job(data: AnalysisInput, user=Depends(current_user)):
    jid=uuid.uuid4().hex[:12]
    with connection() as c:
        c.execute('BEGIN IMMEDIATE');require_data_source(c,user,data.source_id,'analyst')
        if not settings(user)['configured']: raise HTTPException(400,'请联系管理员配置模型并重启服务')
        if not c.execute('SELECT 1 FROM metrics WHERE source_id=?',(data.source_id,)).fetchone():
            raise HTTPException(400,'数据源尚未配置业务指标')
        c.execute('''INSERT INTO analysis_jobs(id,source_id,title,question,report_kind,status,created_by,created)
          VALUES(?,?,?,?,?,?,?,?)''',(jid,data.source_id,data.title,data.question,data.report_kind,'pending',user['id'],time.time()))
        audit(c,user['id'],'analysis_created',jid,data.report_kind)
        c.execute('INSERT INTO analysis_events(job_id,kind,message,created,actor_id) VALUES(?,?,?,?,?)',(jid,'info','分析任务已创建，等待规划',time.time(),user['id']))
    return {'id':jid}

@app.get('/api/analysis-jobs/{jid}')
def get_analysis_job(jid: str, user=Depends(current_user)):
    with connection() as c:
        job,source=analysis_access(c,jid,user);decorate_analysis(c,job,user,source)
        job['source_name']=source['name']
        job['events']=[dict(row) for row in c.execute('''SELECT e.*,u.display_name AS actor_name FROM analysis_events e
          LEFT JOIN users u ON u.id=e.actor_id WHERE job_id=? ORDER BY e.id''',(jid,))]
    job['query_spec']=json.loads(job['query_spec']) if job['query_spec'] else None
    job['result']=json.loads(job['result']) if job['result'] else None
    return job

@app.get('/api/analysis-jobs/{jid}/report')
def export_analysis_report(jid: str, user=Depends(current_user)):
    with connection() as c: job,_=analysis_access(c,jid,user)
    if not job['result']: raise HTTPException(409,'报告尚未生成')
    result=json.loads(job['result'])
    content='# '+job['title']+'\n\n'+result['report']+'\n\n---\n\n数据源：'+result['source']+'\n聚合结果数：'+str(result['row_count'])+'\n'
    return Response(content=content,media_type='text/markdown; charset=utf-8',headers={
        'Content-Disposition':f'attachment; filename="analysis-{jid}.md"'})

@app.get('/api/analysis-jobs/{jid}/evidence')
def export_analysis_evidence(jid: str, user=Depends(current_user)):
    with connection() as c: job,_=analysis_access(c,jid,user)
    if not job['result']: raise HTTPException(409,'指标证据尚未生成')
    rows=json.loads(job['result'])['rows']
    columns=list(dict.fromkeys(key for row in rows for key in row))
    buffer=io.StringIO();writer=csv.DictWriter(buffer,fieldnames=columns,extrasaction='ignore')
    writer.writeheader();writer.writerows(rows)
    return Response(content='\ufeff'+buffer.getvalue(),media_type='text/csv; charset=utf-8',headers={
        'Content-Disposition':f'attachment; filename="evidence-{jid}.csv"'})

@app.post('/api/analysis-jobs/{jid}/plan')
def save_analysis_plan(jid: str, data: AnalysisPlan, user=Depends(current_user)):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE');analysis_access(c,jid,user,'edit')
        changed=c.execute('''UPDATE analysis_jobs SET plan=?,version=version+1,approved=NULL,approved_by=NULL
          WHERE id=? AND status='awaiting_approval' AND version=?''',(data.plan,jid,data.version))
        if changed.rowcount!=1: raise HTTPException(409,'状态或方案版本已变化，请刷新')
        audit(c,user['id'],'analysis_plan_updated',jid,str(data.version+1))
        c.execute('INSERT INTO analysis_events(job_id,kind,message,created,actor_id) VALUES(?,?,?,?,?)',(jid,'info','分析方案已修改，需要重新审批',time.time(),user['id']))
    return {'ok':True}

@app.post('/api/analysis-jobs/{jid}/approve')
def approve_analysis(jid: str, data: Approval, user=Depends(current_user)):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE');analysis_access(c,jid,user,'review')
        changed=c.execute('''UPDATE analysis_jobs SET approved=version,approved_by=?,status='queued'
          WHERE id=? AND status='awaiting_approval' AND version=?''',(user['id'],jid,data.version))
        if changed.rowcount!=1: raise HTTPException(409,'当前分析方案不可批准或版本已变化')
        audit(c,user['id'],'analysis_approved',jid,str(data.version))
        c.execute('INSERT INTO analysis_events(job_id,kind,message,created,actor_id) VALUES(?,?,?,?,?)',(jid,'info','负责人批准分析方案版本 '+str(data.version),time.time(),user['id']))
    return {'ok':True}

@app.post('/api/analysis-jobs/{jid}/cancel')
def cancel_analysis(jid: str, user=Depends(current_user)):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE');analysis_access(c,jid,user,'edit')
        changed=c.execute("UPDATE analysis_jobs SET status='cancelled' WHERE id=? AND status IN ('pending','planning','awaiting_approval','queued','analyzing','reporting')",(jid,))
        if changed.rowcount!=1: raise HTTPException(409,'当前状态不能取消')
        audit(c,user['id'],'analysis_cancelled',jid)
    return {'ok':True}

@app.post('/api/analysis-jobs/{jid}/complete')
def complete_analysis(jid: str, user=Depends(current_user)):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE');analysis_access(c,jid,user,'review')
        changed=c.execute("UPDATE analysis_jobs SET status='completed',completed_by=? WHERE id=? AND status='review'",(user['id'],jid))
        if changed.rowcount!=1: raise HTTPException(409,'只有已经生成报告的任务可以验收')
        audit(c,user['id'],'analysis_completed',jid)
    return {'ok':True}

@app.get('/api/projects')
def projects(user=Depends(current_user)):
    if user['role']=='admin':
        rows=query("SELECT p.*,'maintainer' AS my_role FROM projects p")
    else:
        rows=query('SELECT p.*,m.role AS my_role FROM projects p JOIN project_members m ON m.project_id=p.id WHERE m.user_id=?',(user['id'],))
    return [{**p,'checks':json.loads(p['checks'])} for p in rows]

@app.post('/api/projects')
def create_project(p: Project, user=Depends(current_user)):
    # Repository paths and shell checks grant server execution, so admin-only.
    admin(user)
    path=Path(p.path).expanduser().resolve()
    try:
        root=git(path,'rev-parse','--show-toplevel')
        if Path(root).resolve()!=path:
            raise ValueError('请提供 Git 仓库根目录')
        if p.branch.startswith('-'):
            raise ValueError('基准分支不能以 - 开头')
        git(path,'rev-parse','--verify',p.branch+'^{commit}')
        if not all(x.strip() for x in p.checks):
            raise ValueError('验证命令不能为空')
    except Exception as e:
        raise HTTPException(400,'项目配置无效：'+str(e))
    pid=uuid.uuid4().hex[:12]
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        require_admin(c,user)
        c.execute('INSERT INTO projects(id,name,path,branch,checks,created_by) VALUES(?,?,?,?,?,?)',
                  (pid,p.name,str(path),p.branch,json.dumps(p.checks),user['id']))
        c.execute('INSERT INTO project_members VALUES(?,?,?)',(pid,user['id'],'maintainer'))
        audit(c,user['id'],'project_created',pid,p.name)
    return {'id':pid}

@app.get('/api/projects/{pid}/members')
def members(pid: str, user=Depends(current_user)):
    with connection() as c:
        require_project(c,user,pid,'maintainer')
        return [dict(r) for r in c.execute('''SELECT m.*,u.username,u.display_name,u.active FROM project_members m
            JOIN users u ON u.id=m.user_id WHERE m.project_id=?''',(pid,))]

@app.post('/api/projects/{pid}/members')
def set_member(pid: str, data: Member, user=Depends(current_user)):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        require_project(c,user,pid,'maintainer')
        if not c.execute('SELECT id FROM users WHERE id=? AND active=1',(data.user_id,)).fetchone():
            # Allow removing a deactivated account from the project.
            if data.role!='remove': raise HTTPException(400,'用户不存在或已停用')
        if data.user_id==user['id'] and user['role']!='admin' and data.role!='maintainer':
            raise HTTPException(409,'项目负责人不能移除或降级自己，请联系管理员')
        if data.role=='remove':
            c.execute('DELETE FROM project_members WHERE project_id=? AND user_id=?',(pid,data.user_id))
        else:
            c.execute('INSERT INTO project_members VALUES(?,?,?) ON CONFLICT(project_id,user_id) DO UPDATE SET role=excluded.role',
                      (pid,data.user_id,data.role))
        audit(c,user['id'],'membership_updated',pid,data.user_id+':'+data.role)
    return {'ok':True}


def task_access(c, tid, user, permission='read'):
    t=c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
    if not t:
        raise HTTPException(404,'任务不存在或无权访问')
    p=require_project(c,user,t['project_id'],'maintainer' if permission=='review' else 'viewer')
    if permission=='edit' and p['my_role']!='maintainer':
        if p['my_role']!='developer' or t['created_by']!=user['id']:
            raise HTTPException(403,'只能修改或取消自己创建的任务')
    return dict(t),p


def decorate(c,t,user,p):
    t['can_edit']=p['my_role']=='maintainer' or (p['my_role']=='developer' and t['created_by']==user['id'])
    t['can_review']=p['my_role']=='maintainer'
    for field in ('created_by','approved_by','completed_by'):
        actor=c.execute('SELECT display_name FROM users WHERE id=?',(t[field],)).fetchone()
        t[field+'_name']=actor['display_name'] if actor else '历史记录 / 系统'
    return t

@app.get('/api/tasks')
def tasks(user=Depends(current_user)):
    fields='t.id,t.project_id,t.title,t.requirement,t.acceptance,t.status,t.version,t.created,t.created_by,t.approved_by,t.completed_by'
    if user['role']=='admin':
        rows=query('SELECT '+fields+' FROM tasks t ORDER BY t.created DESC')
    else:
        rows=query('SELECT '+fields+' FROM tasks t JOIN project_members m ON m.project_id=t.project_id WHERE m.user_id=? ORDER BY t.created DESC',(user['id'],))
    return rows

@app.post('/api/tasks')
def create_task(t: Task, user=Depends(current_user)):
    tid=uuid.uuid4().hex[:12]
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        require_project(c,user,t.project_id,'developer')
        if not settings(user)['configured']:
            raise HTTPException(400,'请联系管理员配置模型并重启服务')
        c.execute('INSERT INTO tasks(id,project_id,title,requirement,acceptance,status,created,created_by) VALUES(?,?,?,?,?,?,?,?)',
                  (tid,t.project_id,t.title,t.requirement,t.acceptance,'pending',time.time(),user['id']))
        audit(c,user['id'],'task_created',tid)
    event(tid,'任务已创建，等待分析',actor_id=user['id'])
    return {'id':tid}

@app.get('/api/tasks/{tid}')
def get_task(tid: str, user=Depends(current_user)):
    with connection() as c:
        t,p=task_access(c,tid,user)
        decorate(c,t,user,p)
        t['events']=[dict(r) for r in c.execute('SELECT e.*,u.display_name AS actor_name FROM events e LEFT JOIN users u ON u.id=e.actor_id WHERE task_id=? ORDER BY e.id',(tid,))]
    t['result']=json.loads(t['result']) if t['result'] else None
    return t

@app.post('/api/tasks/{tid}/plan')
def save_plan(tid: str, p: Plan, user=Depends(current_user)):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        task_access(c,tid,user,'edit')
        cur=c.execute("UPDATE tasks SET plan=?,version=version+1,approved=NULL,approved_by=NULL WHERE id=? AND status='awaiting_approval' AND version=?",(p.plan,tid,p.version))
        if cur.rowcount!=1:
            raise HTTPException(409,'状态或方案版本已变化，请刷新')
        audit(c,user['id'],'plan_updated',tid,str(p.version+1))
    event(tid,'方案已修改，需要批准新版本',actor_id=user['id'])
    return {'ok':True}

@app.post('/api/tasks/{tid}/approve')
def approve(tid: str, a: Approval, user=Depends(current_user)):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        task_access(c,tid,user,'review')
        cur=c.execute("UPDATE tasks SET approved=version,approved_by=?,status='queued' WHERE id=? AND status='awaiting_approval' AND version=?",(user['id'],tid,a.version))
        if cur.rowcount!=1:
            raise HTTPException(409,'当前方案不可批准或版本已变化')
        audit(c,user['id'],'plan_approved',tid,str(a.version))
    event(tid,'批准方案版本 '+str(a.version),actor_id=user['id'])
    return {'ok':True}

@app.post('/api/tasks/{tid}/cancel')
def cancel(tid: str, user=Depends(current_user)):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        task_access(c,tid,user,'edit')
        cur=c.execute("UPDATE tasks SET status='cancelled' WHERE id=? AND status IN ('pending','analyzing','awaiting_approval','queued','running','verifying')",(tid,))
        if cur.rowcount!=1:
            raise HTTPException(409,'当前状态不能取消')
        audit(c,user['id'],'task_cancelled',tid)
    event(tid,'用户取消任务',actor_id=user['id'])
    return {'ok':True}

@app.post('/api/tasks/{tid}/complete')
def complete(tid: str, user=Depends(current_user)):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        task_access(c,tid,user,'review')
        cur=c.execute("UPDATE tasks SET status='completed',completed_by=? WHERE id=? AND status='review'",(user['id'],tid))
        if cur.rowcount!=1:
            raise HTTPException(409,'只有通过验证的任务可以验收')
        audit(c,user['id'],'task_completed',tid)
    event(tid,'用户确认代码审查完成；代码未合并',actor_id=user['id'])
    return {'ok':True}

@app.get('/api/tasks/{tid}/stream')
async def stream(tid: str, request: Request, user=Depends(current_user)):
    with connection() as c:
        task_access(c,tid,user)
    async def generate():
        last=-1
        while not await request.is_disconnected():
            # Revalidate live streams after logout, expiry or membership revocation.
            try:
                latest=current_user(request)
                with connection() as c:
                    t,_=task_access(c,tid,latest)
                    rows=[dict(r) for r in c.execute('SELECT id,kind,created FROM events WHERE task_id=? AND id>? ORDER BY id',(tid,last))]
            except HTTPException:
                yield 'event: revoked\ndata: {}\n\n'
                break
            for row in rows:
                last=row['id']
                yield 'data: '+json.dumps(row)+'\n\n'
            yield 'event: status\ndata: '+json.dumps({'status':t['status']})+'\n\n'
            await asyncio.sleep(1)
    return StreamingResponse(generate(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})

# Production can serve the built SPA and API from the same TLS origin.
DIST = ROOT / 'frontend' / 'dist'
if (DIST / 'assets').exists():
    app.mount('/assets', StaticFiles(directory=DIST / 'assets'), name='assets')

@app.get('/{path:path}')
def frontend(path: str):
    if path.startswith('api/') or not (DIST / 'index.html').exists():
        raise HTTPException(404, '页面不存在')
    return FileResponse(DIST / 'index.html')
