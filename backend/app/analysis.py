"""Analysis brief, approval configuration snapshots and verifiable evidence."""
import json
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .analytics import digest, execute_with_evidence, source_configuration_hash, validate_spec
from .auth import audit, current_user
from .core import connection
from .platform import job_source_ids, require_analysis_sources

router=APIRouter(prefix='/api/analysis')


def init_analysis():
    with connection() as c:
        columns={r['name'] for r in c.execute('PRAGMA table_info(analysis_jobs)')}
        for name,definition in (('brief', "TEXT DEFAULT '{}'"),('approval_snapshot','TEXT')):
            if name not in columns: c.execute('ALTER TABLE analysis_jobs ADD COLUMN '+name+' '+definition)


class Brief(BaseModel):
    decision: str=Field(default='',max_length=2000)
    objective: str=Field(default='',max_length=2000)
    constraints: str=Field(default='',max_length=4000)
    assumptions: str=Field(default='',max_length=4000)


class BriefInput(BaseModel):
    version: int=Field(ge=0)
    brief: Brief


def load_catalog(c,job):
    ids=job_source_ids(c,job)
    sources=[]
    for sid in ids:
        row=c.execute('SELECT * FROM data_sources WHERE id=?',(sid,)).fetchone()
        if not row: raise ValueError('数据源已移除，请重新规划')
        sources.append(dict(row))
    metrics=[dict(r) for r in c.execute('''SELECT m.*,t.id AS term_id,t.name AS term_name,
        t.definition AS term_definition,t.unit AS term_unit FROM metrics m
        LEFT JOIN metric_term_links l ON l.metric_id=m.id
        LEFT JOIN metric_terms t ON t.id=l.term_id WHERE m.source_id IN ('''+
        ','.join('?' for _ in ids)+') ORDER BY m.id',ids)]
    return sources,metrics


def _snapshot(c,job):
    sources,metrics=load_catalog(c,job)
    spec=validate_spec(json.loads(job['query_spec']),metrics)
    selected={i['metric_id'] for i in spec['items']}
    actual=[m for m in metrics if m['id'] in selected]
    selected_sources={m['source_id'] for m in actual}
    configs=[{'source_id':s['id'],'configuration_hash':source_configuration_hash(s)}
             for s in sources if s['id'] in selected_sources]
    payload={'version':job['version'],'query':spec,'brief':json.loads(job['brief'] or '{}'),
             'metrics':actual,'sources':configs}
    payload['configuration_hash']=digest(payload)
    return payload,sources,actual,spec


def freeze_approval(c,job):
    snapshot,_,_,_=_snapshot(c,job)
    old=job['approval_snapshot']
    if old and json.loads(old)['configuration_hash']!=snapshot['configuration_hash']:
        raise ValueError('指标、口径或数据源配置已变化，请修改方案并重新审批')
    if not old:
        snapshot['approved_at']=time.time()
        c.execute('UPDATE analysis_jobs SET approval_snapshot=? WHERE id=?',
                  (json.dumps(snapshot,ensure_ascii=False),job['id']))
    else: snapshot=json.loads(old)
    return snapshot


def check_approval_snapshot(c,job):
    old=job['approval_snapshot']
    if not old:
        if job.get('governance_version',0):
            raise ValueError('分析方案缺少配置快照，请重新审批')
        # Legacy approved tasks get a configuration snapshot on their first execution.
        freeze_approval(c,job)
        job=dict(c.execute('SELECT * FROM analysis_jobs WHERE id=?',(job['id'],)).fetchone())
        old=job['approval_snapshot']
    approved=json.loads(old)
    current,sources,metrics,spec=_snapshot(c,job)
    if approved['configuration_hash']!=current['configuration_hash']:
        raise ValueError('批准后指标、口径或连接配置发生变化，执行已停止，请重新审批')
    return sources,metrics,spec,approved


def _access(c,jid,user,edit=False):
    row=c.execute('SELECT * FROM analysis_jobs WHERE id=?',(jid,)).fetchone()
    if not row: raise HTTPException(404,'分析任务不存在')
    job=dict(row);sources=require_analysis_sources(c,user,job,'analyst' if edit else 'viewer')
    if edit and job['created_by']!=user['id'] and not all(s['my_role']=='owner' for s in sources):
        raise HTTPException(403,'只能修改自己创建或负责的分析任务')
    return job


@router.get('/jobs/{jid}/brief')
def get_brief(jid:str,user=Depends(current_user)):
    with connection() as c: job=_access(c,jid,user)
    return {'version':job['version'],'brief':Brief(**json.loads(job['brief'] or '{}')).model_dump()}


@router.post('/jobs/{jid}/brief')
def save_brief(jid:str,data:BriefInput,user=Depends(current_user)):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE');job=_access(c,jid,user,True)
        if job['status']!='awaiting_approval': raise HTTPException(409,'仅待审批任务可修改分析说明')
        if job['version']!=data.version: raise HTTPException(409,'方案版本已变化，请刷新')
        brief=data.brief.model_dump()
        c.execute('UPDATE analysis_jobs SET brief=?,version=version+1,approved=NULL,approved_by=NULL,approval_snapshot=NULL WHERE id=?',
                  (json.dumps(brief,ensure_ascii=False),jid))
        from .orchestration import invalidate_approvals
        invalidate_approvals(c,'analysis',jid)
        audit(c,user['id'],'analysis_brief_updated',jid,str(data.version+1))
        return {'version':data.version+1,'brief':brief}


@router.get('/jobs/{jid}/evidence')
def get_evidence(jid:str,user=Depends(current_user)):
    with connection() as c: job=_access(c,jid,user)
    if not job['result']: raise HTTPException(409,'分析证据尚未生成')
    result=json.loads(job['result'])
    evidence=result.get('evidence',{'limitations':['旧报告未保存来源与质量证据'],'complete':False})
    return {**evidence,'rows':result.get('rows',[]),'claims':result.get('claims',[]),
            'claim_validation':result.get('claim_validation','legacy_unverified'),
            'approval_snapshot':json.loads(job['approval_snapshot']) if job['approval_snapshot'] else None}


init_analysis()
