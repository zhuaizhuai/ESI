"""Versioned resource approvals and atomic claims for the single local worker."""
import json
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import audit, current_user, require_data_source, require_project
from .core import connection
from .platform import job_source_ids, task_project_ids, sync_workflow

router = APIRouter(prefix='/api/orchestration')
KINDS = {'development': ('tasks', 'project', 'maintainer'),
         'analysis': ('analysis_jobs', 'data_source', 'owner')}


def init_orchestration():
    with connection() as c:
        for table in ('tasks', 'analysis_jobs'):
            columns = {r['name'] for r in c.execute('PRAGMA table_info('+table+')')}
            if 'governance_version' not in columns:
                c.execute('ALTER TABLE '+table+' ADD COLUMN governance_version INTEGER NOT NULL DEFAULT 0')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS resource_approvals (
          job_kind TEXT NOT NULL, job_id TEXT NOT NULL, version INTEGER NOT NULL,
          resource_id TEXT NOT NULL, actor_id TEXT NOT NULL, created REAL NOT NULL,
          PRIMARY KEY(job_kind,job_id,version,resource_id)
        );
        CREATE TABLE IF NOT EXISTS runtime_attempts (
          job_kind TEXT NOT NULL, job_id TEXT NOT NULL, attempt INTEGER NOT NULL,
          stage TEXT NOT NULL, started REAL NOT NULL, finished REAL,
          PRIMARY KEY(job_kind,job_id,attempt)
        );
        CREATE TABLE IF NOT EXISTS approved_plans (
          job_kind TEXT NOT NULL, job_id TEXT NOT NULL, version INTEGER NOT NULL,
          payload TEXT NOT NULL, complete INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
          PRIMARY KEY(job_kind,job_id,version)
        );
        ''')


def job_record(c, kind, job_id):
    if kind not in KINDS:
        raise HTTPException(404, '未知任务类型')
    row = c.execute('SELECT * FROM '+KINDS[kind][0]+' WHERE id=?', (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, '任务不存在或无权访问')
    return dict(row)


def resource_ids(c, kind, job):
    return task_project_ids(c, job) if kind == 'development' else job_source_ids(c, job)


def require_resource(c, user, kind, resource_id, elevated=False):
    function = require_project if kind == 'development' else require_data_source
    return function(c, user, resource_id, KINDS[kind][2] if elevated else 'viewer')


def invalidate_approvals(c, kind, job_id):
    c.execute('DELETE FROM resource_approvals WHERE job_kind=? AND job_id=?', (kind, job_id))
    if kind == 'analysis':
        columns = {r['name'] for r in c.execute('PRAGMA table_info(analysis_jobs)')}
        if 'approval_snapshot' in columns:
            c.execute('UPDATE analysis_jobs SET approval_snapshot=NULL WHERE id=?', (job_id,))


def require_execution_approvals(c, kind, job):
    job = dict(job)
    if job['approved'] != job['version'] or not job['approved_by']:
        raise HTTPException(403, '当前方案未完整批准')
    ids = resource_ids(c, kind, job)
    if not job.get('governance_version'):
        actor = c.execute('SELECT * FROM users WHERE id=?', (job['approved_by'],)).fetchone()
        if not actor:
            raise HTTPException(403, '审批人无效')
        for rid in ids:
            require_resource(c, dict(actor), kind, rid, True)
        return
    rows = {row['resource_id']: row for row in c.execute(
        'SELECT * FROM resource_approvals WHERE job_kind=? AND job_id=? AND version=?',
        (kind, job['id'], job['version']))}
    for rid in ids:
        approval = rows.get(rid)
        if not approval:
            raise HTTPException(403, '有资源尚未批准当前方案')
        actor = c.execute('SELECT * FROM users WHERE id=?', (approval['actor_id'],)).fetchone()
        if not actor:
            raise HTTPException(403, '审批人无效')
        require_resource(c, dict(actor), kind, rid, True)


def approval_view(c, kind, job, user):
    ids = resource_ids(c, kind, job)
    approvals = {r['resource_id']: dict(r) for r in c.execute(
        '''SELECT a.*,u.display_name AS approved_by_name FROM resource_approvals a
           LEFT JOIN users u ON u.id=a.actor_id
           WHERE a.job_kind=? AND a.job_id=? AND a.version=?''',
        (kind, job['id'], job['version']))}
    visible = []
    for rid in ids:
        try:
            resource = require_resource(c, user, kind, rid)
        except HTTPException:
            continue
        try:
            require_resource(c, user, kind, rid, True)
            can_approve = job['status'] == 'awaiting_approval'
        except HTTPException:
            can_approve = False
        approval = approvals.get(rid)
        valid = False
        if approval:
            try:
                actor = c.execute('SELECT * FROM users WHERE id=?', (approval['actor_id'],)).fetchone()
                require_resource(c, dict(actor) if actor else {}, kind, rid, True)
                valid = True
            except (HTTPException, KeyError):
                pass
        item = {'resource_id': rid, 'name': resource['name'], 'approved': valid,
                'approved_by_name': approval.get('approved_by_name') if approval else None,
                'can_approve': can_approve}
        if kind == 'development' and job.get('structured_plan'):
            item['scoped_plan'] = next((p for p in json.loads(job['structured_plan']).get('projects', [])
                                        if p['project_id'] == rid), None)
        if kind == 'analysis' and job.get('query_spec'):
            spec = json.loads(job['query_spec'])
            definitions = [dict(m) for m in c.execute('''SELECT m.id,m.name,m.description,m.aggregation,
              m.date_column,m.dimensions,t.name AS unified_name,t.definition AS unified_definition,t.unit
              FROM metrics m LEFT JOIN metric_term_links l ON l.metric_id=m.id
              LEFT JOIN metric_terms t ON t.id=l.term_id WHERE m.source_id=?''', (rid,))]
            metric_ids = {m['id'] for m in definitions}
            item['scoped_plan'] = {key: spec.get(key) for key in ('grain', 'start_date', 'end_date')}
            item['scoped_plan']['items'] = [i for i in spec.get('items', []) if i.get('metric_id') in metric_ids]
            selected = {i['metric_id'] for i in item['scoped_plan']['items']}
            item['scoped_plan']['metric_definitions'] = [
                {**m, 'dimensions': json.loads(m['dimensions'])} for m in definitions if m['id'] in selected]
            item['scoped_plan']['scenarios'] = [s for s in spec.get('scenarios', []) if s.get('metric_id') in selected]
            item['scoped_plan']['comparisons'] = [
                {key:value for key,value in comparison.items() if key not in ('left_metric_id','right_metric_id')}
                | {'local_metric_ids': [mid for mid in (comparison['left_metric_id'],comparison['right_metric_id'])
                                         if mid in selected],
                   'counterpart_visible': comparison['left_metric_id'] in selected and comparison['right_metric_id'] in selected,
                   'left_metric_id': comparison['left_metric_id'] if comparison['left_metric_id'] in selected else None,
                   'right_metric_id': comparison['right_metric_id'] if comparison['right_metric_id'] in selected else None}
                for comparison in spec.get('comparisons', [])
                if comparison['left_metric_id'] in selected or comparison['right_metric_id'] in selected]
            # The shared brief is visible only when the reviewer can read all sources.
            if all(can_read_resource(c,user,kind,source_id) for source_id in ids):
                item['scoped_plan']['brief'] = json.loads(job.get('brief') or '{}')
        visible.append(item)
    if not visible:
        raise HTTPException(404, '任务不存在或无权访问')
    ready = False
    if job['approved'] == job['version']:
        try:
            require_execution_approvals(c, kind, job)
            ready = True
        except HTTPException:
            pass
    return {'version': job['version'], 'resources': visible, 'ready': ready,
            'total_resources': len(ids), 'status': job['status']}


def can_read_resource(c,user,kind,resource_id):
    try:
        require_resource(c,user,kind,resource_id)
        return True
    except HTTPException:
        return False


class ResourceApproval(BaseModel):
    version: int = Field(ge=0)
    resource_ids: list[str] = Field(default_factory=list, max_length=6)


def approve_resources(kind, job_id, data, user, require_all=False):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        job = job_record(c, kind, job_id)
        ids = resource_ids(c, kind, job)
        selected = list(dict.fromkeys(data.resource_ids))
        if require_all:
            # Legacy endpoint preserves its all-resource permission contract.
            for rid in ids:
                require_resource(c, user, kind, rid, True)
            selected = ids
        elif not selected:
            for rid in ids:
                try:
                    require_resource(c, user, kind, rid, True)
                    selected.append(rid)
                except HTTPException:
                    pass
            if not selected:
                if not any_access(c, kind, job, user):
                    raise HTTPException(404, '任务不存在或无权访问')
                raise HTTPException(403, '需要资源负责人权限')
        for rid in selected:
            if rid not in ids:
                raise HTTPException(400, '资源不属于本任务')
            require_resource(c, user, kind, rid, True)
        if job['status'] != 'awaiting_approval' or job['version'] != data.version:
            raise HTTPException(409, '当前方案不可批准或版本已变化')
        if kind == 'development':
            from .development import require_approvable
            require_approvable(c, job)
        else:
            from .analysis import freeze_approval
            try:
                freeze_approval(c, job)
            except (ValueError, TypeError, KeyError) as exc:
                raise HTTPException(409, str(exc)) from exc
        for rid in selected:
            c.execute('''INSERT INTO resource_approvals VALUES(?,?,?,?,?,?)
              ON CONFLICT(job_kind,job_id,version,resource_id) DO UPDATE SET
              actor_id=excluded.actor_id,created=excluded.created''',
                      (kind, job_id, data.version, rid, user['id'], time.time()))
        recorded = {r['resource_id'] for r in c.execute(
            'SELECT resource_id FROM resource_approvals WHERE job_kind=? AND job_id=? AND version=?',
            (kind, job_id, data.version))}
        ready = set(ids) <= recorded
        if ready:
            candidate = {**job, 'approved': job['version'], 'approved_by': user['id'], 'governance_version': 1}
            require_execution_approvals(c, kind, candidate)
            c.execute('UPDATE '+KINDS[kind][0]+" SET approved=version,approved_by=?,status='queued',governance_version=1 WHERE id=?",
                      (user['id'], job_id))
            sync_workflow(c, kind, job_id, 'queued')
        remember_approved_plan(c,kind,job_id,ready)
        audit(c, user['id'], 'resource_plan_approved', job_id,
              json.dumps({'kind': kind, 'version': data.version, 'resources': selected}))
        table, field = ('events', 'task_id') if kind == 'development' else ('analysis_events', 'job_id')
        c.execute('INSERT INTO '+table+'('+field+',kind,message,created,actor_id) VALUES(?,?,?,?,?)',
                  (job_id, 'info', '批准方案版本 '+str(data.version)+' · '+str(len(selected))+' 个资源'+
                   ('，审批齐备等待执行' if ready else '，等待其他负责人'), time.time(), user['id']))
        return {'ok': True, 'ready': ready, 'version': data.version}


def remember_approved_plan(c,kind,job_id,complete):
    job=job_record(c,kind,job_id)
    keys=('title','requirement','acceptance','plan','structured_plan','validation_snapshot') if kind=='development' else (
        'title','question','plan','query_spec','brief','approval_snapshot')
    payload={key:job.get(key) for key in keys}
    for key in ('structured_plan','validation_snapshot','query_spec','brief','approval_snapshot'):
        if payload.get(key):
            payload[key]=json.loads(payload[key])
    payload['approvals']=[dict(row) for row in c.execute('''SELECT resource_id,actor_id,created
      FROM resource_approvals WHERE job_kind=? AND job_id=? AND version=?''',(kind,job_id,job['version']))]
    c.execute('''INSERT INTO approved_plans VALUES(?,?,?,?,?,?)
      ON CONFLICT(job_kind,job_id,version) DO UPDATE SET payload=excluded.payload,complete=excluded.complete''',
              (kind,job_id,job['version'],json.dumps(payload,ensure_ascii=False),int(complete),time.time()))


@router.get('/{kind}/{job_id}/history')
def approved_history(kind: Literal['development','analysis'],job_id:str,user=Depends(current_user)):
    with connection() as c:
        job=job_record(c,kind,job_id)
        for rid in resource_ids(c,kind,job):
            require_resource(c,user,kind,rid)
        return [{**dict(row),'payload':json.loads(row['payload'])} for row in c.execute(
            'SELECT version,payload,complete,created FROM approved_plans WHERE job_kind=? AND job_id=? ORDER BY version',
            (kind,job_id))]


def any_access(c, kind, job, user):
    for rid in resource_ids(c, kind, job):
        try:
            require_resource(c, user, kind, rid)
            return True
        except HTTPException:
            pass
    return False


@router.get('/reviews')
def reviews(user=Depends(current_user)):
    result = []
    with connection() as c:
        for kind, (table, _, _) in KINDS.items():
            for row in c.execute("SELECT * FROM "+table+" WHERE status='awaiting_approval' ORDER BY created"):
                job = dict(row)
                try:
                    view = approval_view(c, kind, job, user)
                except HTTPException:
                    continue
                actionable = [r for r in view['resources'] if r['can_approve'] and not r['approved']]
                if actionable:
                    result.append({'id': job['id'], 'kind': kind, 'title': job['title'],
                                   'version': job['version'], 'resources': actionable})
    return result


@router.get('/inbox')
def approval_inbox(user=Depends(current_user)):
    return {'items': [
        {'kind': job['kind'], 'job_id': job['id'], 'title': job['title'],
         'version': job['version'], **resource}
        for job in reviews(user) for resource in job['resources']
    ]}


@router.get('/{kind}/{job_id}/approvals')
def approvals(kind: Literal['development', 'analysis'], job_id: str, user=Depends(current_user)):
    with connection() as c:
        return approval_view(c, kind, job_record(c, kind, job_id), user)


@router.post('/{kind}/{job_id}/approve')
def approve(kind: Literal['development', 'analysis'], job_id: str, data: ResourceApproval,
            user=Depends(current_user)):
    return approve_resources(kind, job_id, data, user)


@router.post('/{kind}/{job_id}/retry')
def retry(kind: Literal['development', 'analysis'], job_id: str, user=Depends(current_user)):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        job = job_record(c, kind, job_id)
        ids = resource_ids(c, kind, job)
        elevated = True
        for rid in ids:
            resource = require_resource(c, user, kind, rid)
            elevated &= resource['my_role'] == KINDS[kind][2]
        if not elevated:
            if job['created_by'] != user['id']:
                raise HTTPException(403, '只能重试自己创建的任务')
            minimum = 'developer' if kind == 'development' else 'analyst'
            fn = require_project if kind == 'development' else require_data_source
            for rid in ids:
                fn(c, user, rid, minimum)
        if job['status'] not in ('failed', 'interrupted'):
            raise HTTPException(409, '只有失败或中断的任务可以重试')
        attempts = c.execute('SELECT COUNT(*) FROM runtime_attempts WHERE job_kind=? AND job_id=?',
                             (kind, job_id)).fetchone()[0]
        if attempts >= 5:
            raise HTTPException(409, '已达到五次执行上限，请复核后新建任务')
        status = 'pending' if not job.get('plan') else 'awaiting_approval'
        invalidate_approvals(c, kind, job_id)
        # Retry never inherits an old execution authorization; existing worktrees are retained.
        c.execute('UPDATE '+KINDS[kind][0]+' SET status=?,approved=NULL,approved_by=NULL,version=version+1 WHERE id=?',
                  (status, job_id))
        sync_workflow(c, kind, job_id, status)
        audit(c, user['id'], 'task_retried', job_id, kind)
    return {'ok': True, 'status': status}


def claim_next():
    """The SELECT and compare/update share one write transaction; cancellation cannot be resurrected."""
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        candidates = []
        for kind, (table, _, _) in KINDS.items():
            row = c.execute('SELECT * FROM '+table+" WHERE status IN ('pending','queued') ORDER BY created,id LIMIT 1").fetchone()
            if row:
                candidates.append((kind, dict(row)))
        if not candidates:
            return None
        kind, job = min(candidates, key=lambda item: (item[1]['created'], item[1]['id']))
        before = job['status']
        if before == 'queued':
            try:
                require_execution_approvals(c, kind, job)
            except HTTPException as exc:
                c.execute('UPDATE '+KINDS[kind][0]+" SET status='failed' WHERE id=? AND status='queued'", (job['id'],))
                sync_workflow(c, kind, job['id'], 'failed')
                audit(c, None, 'execution_authorization_failed', job['id'], str(exc.detail))
                return None
        status = ('analyzing' if kind == 'development' else 'planning') if before == 'pending' else (
            'running' if kind == 'development' else 'analyzing')
        changed = c.execute('UPDATE '+KINDS[kind][0]+' SET status=? WHERE id=? AND status=?',
                            (status, job['id'], before))
        if changed.rowcount != 1:
            return None
        attempt = c.execute('SELECT COALESCE(MAX(attempt),0)+1 FROM runtime_attempts WHERE job_kind=? AND job_id=?',
                            (kind, job['id'])).fetchone()[0]
        c.execute('INSERT INTO runtime_attempts VALUES(?,?,?,?,?,NULL)',
                  (kind, job['id'], attempt, status, time.time()))
        sync_workflow(c, kind, job['id'], status)
        job['status'] = status
        return kind, job, attempt


def finish_attempt(kind, job_id, attempt):
    with connection() as c:
        c.execute('UPDATE runtime_attempts SET finished=? WHERE job_kind=? AND job_id=? AND attempt=?',
                  (time.time(), kind, job_id, attempt))


init_orchestration()
