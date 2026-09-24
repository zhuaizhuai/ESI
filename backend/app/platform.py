"""Enterprise catalog, shared vocabulary, workflow evidence and advice feedback."""
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import current_user, require_admin, require_data_source, require_project, audit
from .core import connection

router = APIRouter(prefix='/api/platform')


class SystemInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=2000)


class ResourceInput(BaseModel):
    kind: Literal['project', 'data_source']
    resource_id: str


class DependencyInput(BaseModel):
    target_id: str
    description: str = Field(default='', max_length=500)


class KnowledgeInput(BaseModel):
    system_id: str
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=30000)
    source_url: str = Field(default='', max_length=1000)


class MetricTermInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    definition: str = Field(min_length=1, max_length=3000)
    unit: str = Field(default='', max_length=40)


class AssigneeInput(BaseModel):
    assignee_id: str


class FeedbackInput(BaseModel):
    status: Literal['accepted', 'in_progress', 'done', 'dismissed']
    outcome: str = Field(default='', max_length=3000)


def job_source_ids(c, job):
    rows = c.execute('SELECT source_id FROM analysis_job_sources WHERE job_id=? ORDER BY source_id',
                     (job['id'],)).fetchall()
    return [row['source_id'] for row in rows] or [job['source_id']]


def task_project_ids(c, task):
    rows = c.execute('SELECT project_id FROM task_projects WHERE task_id=? ORDER BY project_id',
                     (task['id'],)).fetchall()
    return [row['project_id'] for row in rows] or [task['project_id']]


def require_task_projects(c, user, task, minimum='viewer'):
    return [require_project(c, user, pid, minimum) for pid in task_project_ids(c, task)]


def require_analysis_sources(c, user, job, minimum='viewer'):
    return [require_data_source(c, user, sid, minimum) for sid in job_source_ids(c, job)]


def require_system(c, user, sid, edit=False):
    system = c.execute('SELECT * FROM enterprise_systems WHERE id=?', (sid,)).fetchone()
    if not system:
        raise HTTPException(404, '企业系统不存在或无权访问')
    if user['role'] == 'admin':
        require_admin(c, user)
        return dict(system)
    links = c.execute('SELECT kind,resource_id FROM system_resources WHERE system_id=?',
                      (sid,)).fetchall()
    if not links:
        raise HTTPException(404, '企业系统不存在或无权访问')
    elevated = False
    for link in links:
        if link['kind'] == 'project':
            resource = require_project(c, user, link['resource_id'])
            elevated |= resource['my_role'] == 'maintainer'
        else:
            resource = require_data_source(c, user, link['resource_id'])
            elevated |= resource['my_role'] == 'owner'
    if edit and not elevated:
        raise HTTPException(403, '需要关联项目负责人或数据负责人权限')
    return dict(system)


def visible_systems(c, user):
    result = []
    for row in c.execute('SELECT * FROM enterprise_systems ORDER BY name'):
        try:
            system = require_system(c, user, row['id'])
        except HTTPException:
            continue
        system['resources'] = [dict(link) for link in c.execute(
            'SELECT kind,resource_id FROM system_resources WHERE system_id=? ORDER BY kind',
            (row['id'],))]
        system['dependencies'] = []
        for dep in c.execute('SELECT target_id,description FROM system_dependencies WHERE source_id=?',
                             (row['id'],)):
            try:
                require_system(c, user, dep['target_id'])
                system['dependencies'].append(dict(dep))
            except HTTPException:
                pass
        result.append(system)
    return result


@router.get('/systems')
def list_systems(user=Depends(current_user)):
    with connection() as c:
        return visible_systems(c, user)


@router.post('/systems')
def create_system(data: SystemInput, user=Depends(current_user)):
    sid = uuid.uuid4().hex[:12]
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        require_admin(c, user)
        c.execute('INSERT INTO enterprise_systems VALUES(?,?,?,?,?)',
                  (sid, data.name.strip(), data.description, user['id'], time.time()))
        audit(c, user['id'], 'system_created', sid, data.name)
    return {'id': sid}


@router.post('/systems/{sid}/resources')
def bind_resource(sid: str, data: ResourceInput, user=Depends(current_user)):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        require_admin(c, user)
        if not c.execute('SELECT 1 FROM enterprise_systems WHERE id=?', (sid,)).fetchone():
            raise HTTPException(404, '企业系统不存在')
        table = 'projects' if data.kind == 'project' else 'data_sources'
        if not c.execute(f'SELECT 1 FROM {table} WHERE id=?', (data.resource_id,)).fetchone():
            raise HTTPException(404, '关联资源不存在')
        current = c.execute('SELECT system_id FROM system_resources WHERE kind=? AND resource_id=?',
                            (data.kind, data.resource_id)).fetchone()
        if current and current['system_id'] != sid:
            raise HTTPException(409, '该资源已经关联其他企业系统')
        c.execute('INSERT OR IGNORE INTO system_resources VALUES(?,?,?)',
                  (sid, data.kind, data.resource_id))
        audit(c, user['id'], 'system_resource_bound', sid,
              data.kind + ':' + data.resource_id)
    return {'ok': True}


@router.post('/systems/{sid}/dependencies')
def add_dependency(sid: str, data: DependencyInput, user=Depends(current_user)):
    if sid == data.target_id:
        raise HTTPException(400, '系统不能依赖自身')
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        require_admin(c, user)
        for system_id in (sid, data.target_id):
            if not c.execute('SELECT 1 FROM enterprise_systems WHERE id=?', (system_id,)).fetchone():
                raise HTTPException(404, '企业系统不存在')
        c.execute('''INSERT INTO system_dependencies VALUES(?,?,?)
          ON CONFLICT(source_id,target_id) DO UPDATE SET description=excluded.description''',
                  (sid, data.target_id, data.description))
        audit(c, user['id'], 'system_dependency_updated', sid, data.target_id)
    return {'ok': True}


@router.get('/impacts/{project_id}')
def related_projects(project_id: str, user=Depends(current_user)):
    """Suggest directly connected repositories; selection remains a human choice."""
    with connection() as c:
        require_project(c, user, project_id, 'developer')
        origin = c.execute('''SELECT system_id FROM system_resources
          WHERE kind='project' AND resource_id=?''', (project_id,)).fetchone()
        if not origin:
            return []
        require_system(c, user, origin['system_id'])
        result = []
        for dep in c.execute('''SELECT source_id,target_id,description FROM system_dependencies
          WHERE source_id=? OR target_id=?''', (origin['system_id'], origin['system_id'])):
            target = dep['target_id'] if dep['source_id'] == origin['system_id'] else dep['source_id']
            try:
                system = require_system(c, user, target)
            except HTTPException:
                continue
            for link in c.execute('''SELECT resource_id FROM system_resources
              WHERE system_id=? AND kind='project' ''', (target,)):
                try:
                    project = require_project(c, user, link['resource_id'], 'developer')
                except HTTPException:
                    continue
                if project['id'] != project_id:
                    result.append({'project_id': project['id'], 'project_name': project['name'],
                                   'system_name': system['name'], 'relationship': dep['description']})
        return result


@router.get('/knowledge')
def search_knowledge(system_id: str = '', q: str = '', user=Depends(current_user)):
    with connection() as c:
        allowed = {system['id'] for system in visible_systems(c, user)}
        if system_id and system_id not in allowed:
            raise HTTPException(404, '企业系统不存在或无权访问')
        if not allowed:
            return []
        selected = [system_id] if system_id else sorted(allowed)
        needle = '%' + q.strip()[:100] + '%'
        rows = c.execute('''SELECT * FROM knowledge_entries WHERE system_id IN ('''+
            ','.join('?' for _ in selected)+''') AND (title LIKE ? OR content LIKE ?)
            ORDER BY updated DESC LIMIT 100''', (*selected, needle, needle)).fetchall()
        return [dict(row) for row in rows]


@router.post('/knowledge')
def add_knowledge(data: KnowledgeInput, user=Depends(current_user)):
    kid = uuid.uuid4().hex[:12]
    now = time.time()
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        require_system(c, user, data.system_id, edit=True)
        c.execute('INSERT INTO knowledge_entries VALUES(?,?,?,?,?,?,?,?)',
                  (kid, data.system_id, data.title, data.content, data.source_url,
                   user['id'], now, now))
        audit(c, user['id'], 'knowledge_created', kid, data.system_id)
    return {'id': kid}


@router.get('/metric-terms')
def metric_terms(user=Depends(current_user)):
    with connection() as c:
        terms = []
        for row in c.execute('SELECT * FROM metric_terms ORDER BY name'):
            links = []
            for link in c.execute('''SELECT m.id,m.name,m.source_id FROM metric_term_links l
              JOIN metrics m ON m.id=l.metric_id WHERE l.term_id=?''', (row['id'],)):
                try:
                    require_data_source(c, user, link['source_id'])
                    links.append(dict(link))
                except HTTPException:
                    pass
            if links or user['role'] == 'admin':
                terms.append({**dict(row), 'metrics': links})
        return terms


@router.post('/metric-terms')
def create_metric_term(data: MetricTermInput, user=Depends(current_user)):
    tid = uuid.uuid4().hex[:12]
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        require_admin(c, user)
        try:
            c.execute('INSERT INTO metric_terms VALUES(?,?,?,?,?,?)',
                      (tid, data.name.strip(), data.definition, data.unit, user['id'], time.time()))
        except Exception as exc:
            if 'UNIQUE constraint failed' in str(exc):
                raise HTTPException(409, '指标名称已存在') from exc
            raise
        audit(c, user['id'], 'metric_term_created', tid, data.name)
    return {'id': tid}


@router.post('/metric-terms/{tid}/metrics/{mid}')
def link_metric(tid: str, mid: str, user=Depends(current_user)):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        metric = c.execute('SELECT * FROM metrics WHERE id=?', (mid,)).fetchone()
        if not metric or not c.execute('SELECT 1 FROM metric_terms WHERE id=?', (tid,)).fetchone():
            raise HTTPException(404, '指标或统一口径不存在')
        require_data_source(c, user, metric['source_id'], 'owner')
        c.execute('''INSERT INTO metric_term_links VALUES(?,?)
          ON CONFLICT(metric_id) DO UPDATE SET term_id=excluded.term_id''', (mid, tid))
        audit(c, user['id'], 'metric_term_linked', tid, mid)
    return {'ok': True}


STEPS = {
    'development': [('research', '需求调研'), ('approval', '方案审批'),
                    ('execution', '代码执行'), ('verification', '验证'), ('acceptance', '结果验收')],
    'analysis': [('planning', '分析规划'), ('approval', '方案审批'),
                 ('aggregation', '指标计算'), ('report', '生成建议'), ('acceptance', '结果验收')],
}
ACTIVE_INDEX = {
    'development': {'pending': 0, 'analyzing': 0, 'awaiting_approval': 1,
                    'queued': 2, 'running': 2, 'verifying': 3, 'review': 4, 'completed': 5},
    'analysis': {'pending': 0, 'planning': 0, 'awaiting_approval': 1,
                 'queued': 2, 'analyzing': 2, 'reporting': 3, 'review': 4, 'completed': 5},
}


def sync_workflow(c, kind, job_id, status, persist=True):
    if kind not in STEPS:
        raise ValueError('未知任务类型')
    current = ACTIVE_INDEX[kind].get(status)
    if current is None:
        prior = c.execute('''SELECT position FROM workflow_steps
          WHERE job_kind=? AND job_id=? AND status='active' ORDER BY position DESC LIMIT 1''',
          (kind, job_id)).fetchone()
        current = prior['position'] if prior else 2
    rows = []
    for position, (key, label) in enumerate(STEPS[kind]):
        state = 'done' if position < current else (
            status if status in ('failed','cancelled','interrupted') else 'active'
        ) if position == current else 'pending'
        if persist:
            c.execute('''INSERT INTO workflow_steps VALUES(?,?,?,?,?,?,?)
              ON CONFLICT(job_kind,job_id,step_key) DO UPDATE SET
              status=excluded.status,updated=excluded.updated''',
                      (kind, job_id, key, position, state, label, time.time()))
        rows.append({'key': key, 'name': label, 'position': position, 'status': state})
    return rows


@router.get('/workflows/{kind}/{job_id}')
def workflow(kind: Literal['development', 'analysis'], job_id: str,
             user=Depends(current_user)):
    table = 'tasks' if kind == 'development' else 'analysis_jobs'
    with connection() as c:
        job = c.execute(f'SELECT * FROM {table} WHERE id=?', (job_id,)).fetchone()
        if not job:
            raise HTTPException(404, '任务不存在或无权访问')
        if kind == 'development':
            require_task_projects(c, user, job)
        else:
            require_analysis_sources(c, user, job)
        return sync_workflow(c, kind, job_id, job['status'], persist=False)


def analysis_job(c, user, job_id, minimum='viewer'):
    job = c.execute('SELECT * FROM analysis_jobs WHERE id=?', (job_id,)).fetchone()
    if not job:
        raise HTTPException(404, '分析任务不存在或无权访问')
    require_analysis_sources(c, user, job, minimum)
    return job


@router.get('/analysis-jobs/{job_id}/recommendations')
def list_recommendations(job_id: str, user=Depends(current_user)):
    with connection() as c:
        job=analysis_job(c, user, job_id)
        full=job['created_by']==user['id']
        try:
            require_analysis_sources(c,user,job,'owner')
            full=True
        except HTTPException:
            pass
        rows=c.execute('''SELECT r.*,u.display_name AS assignee_name
          FROM recommendations r LEFT JOIN users u ON u.id=r.assignee_id
          WHERE r.job_id=? ORDER BY r.created''', (job_id,))
        return [dict(row) for row in rows if full or row['assignee_id']==user['id']]


@router.post('/recommendations/{rid}/assign')
def assign_recommendation(rid: str, data: AssigneeInput, user=Depends(current_user)):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        rec = c.execute('SELECT * FROM recommendations WHERE id=?', (rid,)).fetchone()
        if not rec:
            raise HTTPException(404, '建议不存在')
        job = analysis_job(c, user, rec['job_id'], 'owner')
        target = c.execute('SELECT * FROM users WHERE id=? AND active=1',
                           (data.assignee_id,)).fetchone()
        if not target:
            raise HTTPException(404, '员工不存在或已停用')
        require_analysis_sources(c, dict(target), job)
        c.execute('UPDATE recommendations SET assignee_id=?,updated=? WHERE id=?',
                  (data.assignee_id, time.time(), rid))
        audit(c, user['id'], 'recommendation_assigned', rid, data.assignee_id)
    return {'ok': True}


@router.post('/recommendations/{rid}/feedback')
def feedback_recommendation(rid: str, data: FeedbackInput, user=Depends(current_user)):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        rec = c.execute('SELECT * FROM recommendations WHERE id=?', (rid,)).fetchone()
        if not rec:
            raise HTTPException(404, '建议不存在')
        job = analysis_job(c, user, rec['job_id'])
        owner = False
        try:
            require_analysis_sources(c, user, job, 'owner')
            owner = True
        except HTTPException:
            pass
        if not owner and rec['assignee_id'] != user['id']:
            raise HTTPException(403, '只有负责人或被分配员工可以反馈')
        if rec['status'] in ('done', 'dismissed'):
            raise HTTPException(409, '建议已经结束')
        if data.status == 'done' and not data.outcome.strip():
            raise HTTPException(400, '完成时请填写实际结果')
        c.execute('UPDATE recommendations SET status=?,outcome=?,updated=? WHERE id=?',
                  (data.status, data.outcome, time.time(), rid))
        audit(c, user['id'], 'recommendation_feedback', rid, data.status)
    return {'ok': True}
