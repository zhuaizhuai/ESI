"""Employee actions, private inbox, effect measurements and development handoff."""
import json
import hashlib
import math
import time
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import audit, current_user, require_data_source, require_project
from .core import connection
from .platform import job_source_ids, require_analysis_sources, sync_workflow

router = APIRouter(prefix='/api/actions')


def init_actions():
    with connection() as c:
        columns = {r['name'] for r in c.execute('PRAGMA table_info(recommendations)')}
        fields = {'due_at': 'REAL', 'metric_id': 'TEXT', 'baseline': 'REAL', 'target': 'REAL',
                  'direction': "TEXT NOT NULL DEFAULT 'increase'",
                  'dimensions': "TEXT NOT NULL DEFAULT '{}'", 'measurement': 'TEXT',
                  'development_task_id': 'TEXT', 'metric_snapshot': 'TEXT',
                  'start_date': 'TEXT', 'end_date': 'TEXT'}
        for key, definition in fields.items():
            if key not in columns:
                c.execute('ALTER TABLE recommendations ADD COLUMN '+key+' '+definition)
        c.executescript('''
        CREATE TABLE IF NOT EXISTS action_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, recommendation_id TEXT NOT NULL,
          actor_id TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS action_events_recommendation ON action_events(recommendation_id,id);
        CREATE TABLE IF NOT EXISTS notifications (
          id TEXT PRIMARY KEY, user_id TEXT NOT NULL, recommendation_id TEXT NOT NULL,
          message TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS notifications_user ON notifications(user_id,read,created);
        CREATE INDEX IF NOT EXISTS recommendations_assignee ON recommendations(assignee_id,status,due_at);
        ''')


def record_action(c, rec_id, user_id, action, detail):
    encoded = json.dumps(detail, ensure_ascii=False, allow_nan=False)
    c.execute('INSERT INTO action_events(recommendation_id,actor_id,action,detail,created) VALUES(?,?,?,?,?)',
              (rec_id, user_id, action, encoded, time.time()))
    audit(c, user_id, 'action_'+action, rec_id, encoded[:16000])


def notify(c, user_id, rec_id, message):
    if user_id:
        c.execute('INSERT INTO notifications VALUES(?,?,?,?,0,?)',
                  (uuid.uuid4().hex[:12], user_id, rec_id, message[:500], time.time()))


def access_action(c, user, rid, manage=False):
    rec = c.execute('SELECT * FROM recommendations WHERE id=?', (rid,)).fetchone()
    if not rec:
        raise HTTPException(404, '行动建议不存在或无权访问')
    job = c.execute('SELECT * FROM analysis_jobs WHERE id=?', (rec['job_id'],)).fetchone()
    if not job:
        raise HTTPException(404, '分析任务不存在或无权访问')
    owner = False
    try:
        require_analysis_sources(c, user, job, 'owner')
        owner = True
    except HTTPException:
        pass
    if not owner and rec['assignee_id'] != user['id']:
        try:
            require_analysis_sources(c, user, job)
        except HTTPException:
            raise HTTPException(404, '行动建议不存在或无权访问') from None
        raise HTTPException(403, '只有负责人或被分配员工可以操作')
    if manage and not owner:
        raise HTTPException(403, '需要全部关联数据源的负责人权限')
    return dict(rec), dict(job), owner


def present_action(c, rec, owner=False):
    item = dict(rec)
    actor = c.execute('SELECT display_name FROM users WHERE id=?', (item['assignee_id'],)).fetchone()
    item['assignee_name'] = actor['display_name'] if actor else None
    for field in ('dimensions', 'measurement'):
        item[field] = json.loads(item[field]) if item.get(field) else ({} if field == 'dimensions' else None)
    item.pop('metric_snapshot', None)
    item['overdue'] = bool(item['due_at'] and item['due_at'] < time.time() and
                           item['status'] not in ('done', 'dismissed'))
    item.update(can_configure=owner, can_measure=owner and bool(item['metric_id']),
                can_development=owner and not bool(item['development_task_id']),
                can_feedback=item['status'] not in ('done', 'dismissed'))
    item['history'] = [{**dict(r), 'detail': json.loads(r['detail'])} for r in c.execute(
        'SELECT id,action,detail,created FROM action_events WHERE recommendation_id=? ORDER BY id', (item['id'],))]
    if not owner:
        # Assignment grants action context and outcomes, not the source catalog or execution evidence.
        item.pop('metric_id', None)
        item.pop('dimensions', None)
        if item['measurement']:
            item['measurement'] = public_measurement(item['measurement'])
        for history in item['history']:
            if history['action'] == 'measured':
                history['detail'] = public_measurement(history['detail'])
            elif history['action'] == 'configured':
                history['detail'] = {key:value for key,value in history['detail'].items()
                                     if key in ('assignee_id','due_at','baseline','target','direction','start_date','end_date')}
            elif history['action'] == 'development_created':
                history['detail'] = {'task_id': history['detail'].get('task_id')}
    return item


def public_measurement(value):
    return {key:item for key,item in value.items() if key in (
        'value','baseline','target','change','target_met','measured_at','limitations','start_date','end_date')}


def measurement_configuration(c,metric_id):
    from .analytics import source_configuration_hash
    metric=c.execute('''SELECT m.*,t.id AS term_id,t.name AS term_name,
      t.definition AS term_definition,t.unit AS term_unit FROM metrics m
      LEFT JOIN metric_term_links l ON l.metric_id=m.id
      LEFT JOIN metric_terms t ON t.id=l.term_id WHERE m.id=?''',(metric_id,)).fetchone()
    if not metric:
        raise HTTPException(400,'衡量指标不存在')
    source=c.execute('SELECT * FROM data_sources WHERE id=?',(metric['source_id'],)).fetchone()
    if not source:
        raise HTTPException(400,'衡量数据源不存在')
    return {'metric':dict(metric),'source_configuration_hash':source_configuration_hash(dict(source))}


@router.get('/inbox')
def inbox(user=Depends(current_user)):
    remind_due(user['id'])
    with connection() as c:
        items = []
        for rec in c.execute('SELECT * FROM recommendations WHERE assignee_id=? ORDER BY due_at IS NULL,due_at,created DESC',
                             (user['id'],)):
            _, _, owner = access_action(c, user, rec['id'])
            items.append(present_action(c, rec, owner))
        notices = [dict(row) for row in c.execute(
            'SELECT id,message,read,created,recommendation_id FROM notifications WHERE user_id=? ORDER BY created DESC LIMIT 100',
            (user['id'],))]
    return {'items': items, 'notifications': notices}


def remind_due(user_id=None):
    """One internal overdue reminder per employee/action/day, also refreshed on inbox visits."""
    now=time.time()
    day=time.strftime('%Y-%m-%d',time.gmtime(now))
    with connection() as c:
        sql='''SELECT r.id,r.title,r.assignee_id FROM recommendations r JOIN users u ON u.id=r.assignee_id
          WHERE r.due_at<? AND r.status NOT IN ('done','dismissed') AND u.active=1'''
        params=[now]
        if user_id:
            sql+=' AND r.assignee_id=?'
            params.append(user_id)
        for rec in c.execute(sql,params).fetchall():
            nid=hashlib.sha256((rec['assignee_id']+':'+rec['id']+':due:'+day).encode()).hexdigest()[:24]
            c.execute('INSERT OR IGNORE INTO notifications VALUES(?,?,?,?,0,?)',
                      (nid,rec['assignee_id'],rec['id'],'行动已逾期：'+rec['title'],now))


@router.post('/notifications/{nid}/read')
def read_notification(nid: str, user=Depends(current_user)):
    with connection() as c:
        changed = c.execute('UPDATE notifications SET read=1 WHERE id=? AND user_id=?', (nid, user['id']))
        if changed.rowcount != 1:
            raise HTTPException(404, '通知不存在')
    return {'ok': True}


class ActionConfiguration(BaseModel):
    assignee_id: Optional[str] = None
    due_at: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    metric_id: Optional[str] = None
    baseline: Optional[float] = Field(default=None, allow_inf_nan=False)
    target: Optional[float] = Field(default=None, allow_inf_nan=False)
    direction: Literal['increase', 'decrease'] = 'increase'
    dimensions: dict = Field(default_factory=dict)
    start_date: Optional[str] = None
    end_date: Optional[str] = None


def configure_action(rid, data, user):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        rec, job, _ = access_action(c, user, rid, True)
        if rec['status'] in ('done', 'dismissed'):
            raise HTTPException(409, '已结束建议不能重新配置')
        if data.assignee_id:
            target = c.execute('SELECT * FROM users WHERE id=? AND active=1 AND must_change_password=0',
                               (data.assignee_id,)).fetchone()
            if not target:
                raise HTTPException(400, '员工不存在、已停用或尚未完成初始改密')
        snapshot = None
        if data.metric_id:
            metric = c.execute('SELECT * FROM metrics WHERE id=?', (data.metric_id,)).fetchone()
            if not metric or metric['source_id'] not in job_source_ids(c, job):
                raise HTTPException(400, '衡量指标必须属于原分析任务的数据源')
            require_data_source(c, user, metric['source_id'], 'owner')
            allowed = json.loads(metric['dimensions'])
            if any(key not in allowed for key in data.dimensions) or len(data.dimensions) > 2:
                raise HTTPException(400, '衡量维度不属于指标允许范围或超过两个')
            if any(not isinstance(v,(str,int,float,bool,type(None))) or len(str(v))>500
                   for v in data.dimensions.values()):
                raise HTTPException(400, '维度仅支持有限长度的简单值')
            if any(isinstance(v, float) and not math.isfinite(v) for v in data.dimensions.values()):
                raise HTTPException(400, '维度值必须有限')
            from .analytics import validate_spec
            try:
                validate_spec({'items':[{'metric_id':metric['id'],'dimensions':list(data.dimensions)}],
                               'grain':'none','start_date':data.start_date,'end_date':data.end_date},[dict(metric)])
            except (ValueError, TypeError) as exc:
                raise HTTPException(400, str(exc)) from exc
            snapshot = json.dumps(measurement_configuration(c,metric['id']), ensure_ascii=False)
        elif data.target is not None or data.baseline is not None or data.dimensions or data.start_date or data.end_date:
            raise HTTPException(400, '设置基线、目标或维度前请先选择衡量指标')
        c.execute('''UPDATE recommendations SET assignee_id=?,due_at=?,metric_id=?,baseline=?,target=?,
          direction=?,dimensions=?,metric_snapshot=?,start_date=?,end_date=?,measurement=NULL,updated=? WHERE id=?''',
                  (data.assignee_id, data.due_at, data.metric_id, data.baseline, data.target,
                   data.direction, json.dumps(data.dimensions, ensure_ascii=False, allow_nan=False),
                   snapshot, data.start_date, data.end_date, time.time(), rid))
        record_action(c, rid, user['id'], 'configured', data.model_dump())
        if data.assignee_id:
            notify(c, data.assignee_id, rid, '待办已分配或更新：'+rec['title'])
        if rec['assignee_id'] and rec['assignee_id'] != data.assignee_id:
            notify(c, rec['assignee_id'], rid, '待办已移交：'+rec['title'])
    return {'ok': True}


@router.post('/{rid}/configure')
def configure(rid: str, data: ActionConfiguration, user=Depends(current_user)):
    return configure_action(rid, data, user)


class ActionFeedback(BaseModel):
    status: Literal['accepted', 'in_progress', 'done', 'dismissed']
    outcome: str = Field(default='', max_length=3000)


def feedback_action(rid, data, user):
    transitions = {'proposed': {'accepted', 'in_progress', 'done', 'dismissed'},
                   'accepted': {'in_progress', 'done', 'dismissed'},
                   'in_progress': {'in_progress', 'done', 'dismissed'}}
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        rec, job, _ = access_action(c, user, rid)
        if data.status not in transitions.get(rec['status'], set()):
            raise HTTPException(409, '当前状态不能进行该操作')
        if data.status in ('done', 'dismissed') and not data.outcome.strip():
            raise HTTPException(400, '完成或不采纳时请填写结果和依据')
        c.execute('UPDATE recommendations SET status=?,outcome=?,updated=? WHERE id=?',
                  (data.status, data.outcome, time.time(), rid))
        record_action(c, rid, user['id'], 'feedback', data.model_dump())
        if job['created_by'] != user['id']:
            notify(c, job['created_by'], rid, '行动有新反馈：'+rec['title'])
    return {'ok': True}


@router.post('/{rid}/feedback')
def feedback(rid: str, data: ActionFeedback, user=Depends(current_user)):
    return feedback_action(rid, data, user)


@router.post('/{rid}/measure')
def measure(rid: str, user=Depends(current_user)):
    # The external read does not hold a database write lock.
    with connection() as c:
        rec, job, _ = access_action(c, user, rid, True)
        metric = c.execute('SELECT * FROM metrics WHERE id=?', (rec['metric_id'],)).fetchone()
        if not metric:
            raise HTTPException(400, '请先配置衡量指标')
        configuration=measurement_configuration(c,metric['id'])
        if not rec['metric_snapshot'] or configuration != json.loads(rec['metric_snapshot']):
            raise HTTPException(409, '衡量指标、口径或连接配置已变化，请重新配置基线和目标')
        source = dict(require_data_source(c, user, metric['source_id'], 'owner'))
        metric = configuration['metric']
    from .analytics import execute_with_evidence
    dims = json.loads(rec['dimensions'])
    spec = {'items': [{'metric_id': metric['id'], 'dimensions': list(dims)}], 'grain': 'none',
            'start_date': rec['start_date'], 'end_date': rec['end_date']}
    try:
        _, rows, evidence = execute_with_evidence([source], [metric], spec)
    except (ValueError, TypeError, OSError) as exc:
        raise HTTPException(400, '效果复测失败：'+str(exc)) from exc
    def comparable(value):
        return type(value).__name__, value
    selected = [r for r in rows if r.get('metric_id') == metric['id'] and
                all(comparable(r.get(k)) == comparable(v) for k, v in dims.items())]
    if len(selected) != 1 or selected[0].get('value') is None:
        raise HTTPException(400, '没有唯一可用的指标结果，请核对维度范围')
    current = selected[0]['value']
    measurement = {'value': current, 'baseline': rec['baseline'], 'target': rec['target'],
                   'change': current-rec['baseline'] if rec['baseline'] is not None else None,
                   'target_met': None if rec['target'] is None else
                                 current >= rec['target'] if rec['direction'] == 'increase' else current <= rec['target'],
                   'measured_at': time.time(), 'evidence': evidence,
                   'start_date': rec['start_date'], 'end_date': rec['end_date'],
                   'limitations': ['前后指标变化不等同于行动造成的因果效果；需控制时间范围和其他因素。']}
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        fresh, _, _ = access_action(c, user, rid, True)
        if fresh['updated'] != rec['updated']:
            raise HTTPException(409, '行动配置或反馈已变化，请重新复测')
        if measurement_configuration(c,metric['id']) != configuration:
            raise HTTPException(409, '复测期间指标、口径或连接配置已变化')
        c.execute('UPDATE recommendations SET measurement=?,updated=? WHERE id=?',
                  (json.dumps(measurement, ensure_ascii=False, allow_nan=False), time.time(), rid))
        record_action(c, rid, user['id'], 'measured', measurement)
        notify(c, fresh['assignee_id'], rid, '行动效果已复测：'+fresh['title'])
    return measurement


class DevelopmentHandoff(BaseModel):
    project_ids: list[str] = Field(min_length=1, max_length=6)
    title: str = Field(min_length=1, max_length=200)
    requirement: str = Field(min_length=1, max_length=20000)
    acceptance: str = Field(min_length=1, max_length=10000)


@router.post('/{rid}/development')
def development_handoff(rid: str, data: DevelopmentHandoff, user=Depends(current_user)):
    ids = list(dict.fromkeys(data.project_ids))
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        rec, _, _ = access_action(c, user, rid, True)
        if rec['development_task_id']:
            # Idempotent handoff still checks the caller can access the linked resources.
            from .platform import task_project_ids
            prior = c.execute('SELECT * FROM tasks WHERE id=?', (rec['development_task_id'],)).fetchone()
            if not prior:
                raise HTTPException(409, '已关联研发任务不存在，请联系管理员')
            original_ids = task_project_ids(c, prior)
            for rid_project in original_ids:
                require_project(c, user, rid_project, 'developer')
            if set(ids) != set(original_ids) or data.title != prior['title'] or data.acceptance != prior['acceptance']:
                raise HTTPException(409, '建议已转为研发任务，不能更换目标或重复创建')
            return {'id': rec['development_task_id'], 'existing': True}
        for pid in ids:
            require_project(c, user, pid, 'developer')
        from .main import settings
        if not settings(user)['configured']:
            raise HTTPException(400, '请先配置模型')
        tid = uuid.uuid4().hex[:12]
        requirement = data.requirement+'\n\n行动来源：'+rec['title']+'\n'+rec['action']
        if len(requirement) > 20000:
            raise HTTPException(400, '需求和行动来源合计超过长度限制')
        c.execute('''INSERT INTO tasks(id,project_id,title,requirement,acceptance,status,created,created_by,
          plan_schema,governance_version) VALUES(?,?,?,?,?,'pending',?,?,1,1)''',
                  (tid, ids[0], data.title, requirement, data.acceptance, time.time(), user['id']))
        c.executemany('INSERT INTO task_projects VALUES(?,?)', [(tid, pid) for pid in ids])
        sync_workflow(c, 'development', tid, 'pending')
        c.execute('UPDATE recommendations SET development_task_id=?,updated=? WHERE id=?', (tid, time.time(), rid))
        record_action(c, rid, user['id'], 'development_created', {'task_id': tid, 'projects': ids})
    return {'id': tid, 'existing': False}


init_actions()
