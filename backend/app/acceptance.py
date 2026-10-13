"""Resource-scoped acceptance with live authority and result fingerprints."""
import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import audit, current_user
from .core import connection
from .development import changed_files, working_content_hash
from .orchestration import KINDS, job_record, resource_ids, require_resource
from .platform import sync_workflow

router = APIRouter(prefix='/api/acceptance')


def init_acceptance():
    with connection() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS resource_acceptances (
          job_kind TEXT NOT NULL, job_id TEXT NOT NULL, version INTEGER NOT NULL,
          resource_id TEXT NOT NULL, actor_id TEXT NOT NULL, result_hash TEXT NOT NULL,
          created REAL NOT NULL,
          PRIMARY KEY(job_kind,job_id,version,resource_id))''')


class AcceptanceInput(BaseModel):
    version: int = Field(ge=0)
    resource_ids: list[str] = Field(default_factory=list, max_length=6)


def _result(job, kind):
    try:
        result = json.loads(job['result']) if job.get('result') else None
    except (ValueError, TypeError):
        result = None
    if not isinstance(result, dict) or not result:
        raise HTTPException(409, '尚无可验收的执行结果')
    if kind == 'development' and (not isinstance(result.get('checks', []), list)
            or any(not isinstance(check, dict) for check in result.get('checks', []))):
        raise HTTPException(409, '验证结果格式无效')
    if result.get('claim_validation') == 'model_pending':
        raise HTTPException(409, '分析报告尚未完成，不能验收')
    if kind == 'analysis' and (not isinstance(result.get('report'), str) or not result['report'].strip()):
        raise HTTPException(409, '分析报告尚未完成，不能验收')
    if kind == 'analysis':
        evidence = result.get('evidence') or {}
        if (not isinstance(result.get('rows', []), list)
                or not isinstance(evidence, dict)
                or not isinstance(evidence.get('quality', {}), dict)
                or any(not isinstance(evidence.get(field, []), list) for field in ('sources', 'metrics'))):
            raise HTTPException(409, '分析证据格式无效')
    return result


def scoped_result(c, kind, job, rid, result=None):
    result = result or _result(job, kind)
    ids = resource_ids(c, kind, job)
    single = len(ids) == 1
    if kind == 'analysis':
        rows = [row for row in result.get('rows', []) if isinstance(row, dict)
                and (row.get('source_id') == rid or single and not row.get('source_id'))]
        evidence = result.get('evidence') or {}
        metrics = [metric for metric in evidence.get('metrics', []) if isinstance(metric, dict)
                   and (metric.get('source_id') == rid or single and not metric.get('source_id'))]
        metric_ids = {metric['id'] for metric in metrics if metric.get('id')}
        metric_ids.update(row['metric_id'] for row in rows if row.get('metric_id'))
        quality = {mid: value for mid, value in evidence.get('quality', {}).items() if mid in metric_ids}
        source = next((source for source in evidence.get('sources', []) if source.get('source_id') == rid), None)
        if not source:
            source = {'source_id': rid, 'complete': False}
        return {'rows': rows, 'metrics': metrics, 'quality': quality, 'source': source,
                'limitations': evidence.get('limitations', ['旧报告未保存来源与质量证据']),
                'claim_validation': result.get('claim_validation', 'legacy_report')}
    checks = [check for check in result.get('checks', []) if isinstance(check, dict)
              and (check.get('project_id') == rid or single and not check.get('project_id'))]
    run = c.execute('SELECT * FROM development_runs WHERE task_id=? AND project_id=?', (job['id'], rid)).fetchone()
    tree = c.execute('SELECT base,worktree FROM task_project_runs WHERE task_id=? AND project_id=?', (job['id'], rid)).fetchone()
    base = tree['base'] if tree else run['base'] if run else job.get('base') if single else None
    verified_hash = result.get('verification_hashes', {}).get(rid)
    if job.get('plan_schema', 0) and not verified_hash:
        raise HTTPException(409, '本仓缺少代码验证指纹，请重新执行验证')
    if verified_hash:
        if not tree or not Path(tree['worktree']).exists() or not base:
            raise HTTPException(409, '本仓工作区不可用，请恢复并重新验证')
        try:
            current_hash = working_content_hash(tree['worktree'], base)
        except Exception:
            raise HTTPException(409, '本仓验证指纹无法读取，请重新验证')
        if current_hash != verified_hash:
            raise HTTPException(409, '本仓代码在验证后发生变化，请重新执行验证')
    files = json.loads(run['changed_files']) if run else []
    diff = result.get('diff', '') if single else ''
    if tree and Path(tree['worktree']).exists():
        try:
            files = changed_files(tree['worktree'], base)
            diff = subprocess.check_output(['git', '-C', tree['worktree'], 'diff', '--no-ext-diff',
                '--no-textconv', base, '--'], text=True, stderr=subprocess.STDOUT, timeout=30)
        except Exception:
            raise HTTPException(409, '本仓工作区无法读取，请恢复交付包后验收')
    elif not single:
        # Never expose the combined multi-repository diff through a resource-only review.
        diff = ''
    if not checks and run:
        checks = json.loads(run['checks'])
    if not checks or any(check.get('exit_code') != 0 for check in checks):
        raise HTTPException(409, '本仓缺少通过的验证结果')
    content_hash = hashlib.sha256(diff.encode())
    if tree and Path(tree['worktree']).exists():
        root = Path(tree['worktree']).resolve()
        for name in files:
            path = (root / name).resolve()
            if root not in path.parents:
                raise HTTPException(409, '本仓交付文件路径无效')
            content_hash.update(name.encode())
            if path.is_file():
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(65536), b''):
                        content_hash.update(chunk)
    return {'base': base, 'checks': checks, 'changed_files': files,
            'diff': diff[:100000], 'diff_truncated': len(diff) > 100000,
            'content_hash': content_hash.hexdigest()}


def result_fingerprint(c, kind, job, rid, result=None, scoped=None):
    result = result or _result(job, kind)
    scoped = scoped or scoped_result(c, kind, job, rid, result)
    # Hash the whole result without returning it. A changed overall deliverable invalidates prior sign-offs.
    payload = {'version': job['version'], 'resource_id': rid, 'result': result, 'scoped': scoped}
    try:
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    except (TypeError, ValueError):
        raise HTTPException(409, '执行结果格式无效，不能验收')
    return hashlib.sha256(encoded).hexdigest()


def _valid_acceptance(c, kind, job, rid, recorded, result, scoped=None):
    if not recorded or recorded['version'] != job['version']:
        return False
    actor = c.execute('SELECT * FROM users WHERE id=?', (recorded['actor_id'],)).fetchone()
    try:
        if not actor:
            return False
        require_resource(c, dict(actor), kind, rid, True)
        return recorded['result_hash'] == result_fingerprint(c, kind, job, rid, result, scoped)
    except HTTPException:
        return False


@router.get('/inbox')
def inbox(user=Depends(current_user)):
    items = []
    with connection() as c:
        for kind, (table, _, _) in KINDS.items():
            for row in c.execute('SELECT * FROM ' + table + " WHERE status='review' ORDER BY created").fetchall():
                job = dict(row)
                for rid in resource_ids(c, kind, job):
                    try:
                        resource = require_resource(c, user, kind, rid, True)
                        result = _result(job, kind)
                        scoped = scoped_result(c, kind, job, rid, result)
                        recorded = c.execute('''SELECT * FROM resource_acceptances
                          WHERE job_kind=? AND job_id=? AND version=? AND resource_id=?''',
                            (kind, job['id'], job['version'], rid)).fetchone()
                        if _valid_acceptance(c, kind, job, rid, recorded, result, scoped):
                            continue
                        items.append({'kind': kind, 'job_id': job['id'], 'title': job['title'],
                            'version': job['version'], 'resource_id': rid, 'name': resource['name'],
                            'scoped_result': scoped, 'can_accept': True})
                    except HTTPException:
                        continue
    return {'items': items}


def accept_resources(kind, job_id, data, user, require_all=False):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        job = job_record(c, kind, job_id)
        ids = resource_ids(c, kind, job)
        selected = list(dict.fromkeys(data.resource_ids))
        if require_all:
            selected = ids
        elif not selected:
            for rid in ids:
                try:
                    require_resource(c, user, kind, rid, True)
                    selected.append(rid)
                except HTTPException:
                    continue
            if not selected:
                # Distinguish read-only access from a completely hidden task without leaking other resources.
                visible = False
                for rid in ids:
                    try:
                        require_resource(c, user, kind, rid)
                        visible = True
                    except HTTPException:
                        pass
                raise HTTPException(403 if visible else 404, '需要资源负责人验收权限' if visible else '任务不存在或无权访问')
        for rid in selected:
            if rid not in ids:
                raise HTTPException(400, '资源不属于本任务')
            require_resource(c, user, kind, rid, True)
        if job['status'] != 'review' or job['version'] != data.version:
            raise HTTPException(409, '结果状态或版本已变化，请刷新')
        result = _result(job, kind)
        for rid in selected:
            fingerprint = result_fingerprint(c, kind, job, rid, result)
            c.execute('''INSERT INTO resource_acceptances VALUES(?,?,?,?,?,?,?)
              ON CONFLICT(job_kind,job_id,version,resource_id) DO UPDATE SET
              actor_id=excluded.actor_id,result_hash=excluded.result_hash,created=excluded.created''',
              (kind, job_id, job['version'], rid, user['id'], fingerprint, time.time()))
        recorded = {row['resource_id']: row for row in c.execute('''SELECT * FROM resource_acceptances
          WHERE job_kind=? AND job_id=? AND version=?''', (kind, job_id, job['version']))}
        completed = all(_valid_acceptance(c, kind, job, rid, recorded.get(rid), result) for rid in ids)
        if completed:
            c.execute('UPDATE ' + KINDS[kind][0] + " SET status='completed',completed_by=? WHERE id=? AND status='review' AND version=?",
                      (user['id'], job_id, job['version']))
            sync_workflow(c, kind, job_id, 'completed')
        audit(c, user['id'], 'resource_result_accepted', job_id,
              json.dumps({'kind': kind, 'version': job['version'], 'resources': selected, 'completed': completed}))
        event_table, field = ('events', 'task_id') if kind == 'development' else ('analysis_events', 'job_id')
        c.execute('INSERT INTO ' + event_table + '(' + field + ',kind,message,created,actor_id) VALUES(?,?,?,?,?)',
                  (job_id, 'info', '验收版本 ' + str(job['version']) + ' · ' + str(len(selected)) + ' 个资源' +
                   ('，所有资源验收齐备' if completed else '，等待其他资源验收'), time.time(), user['id']))
    return {'ok': True, 'completed': completed, 'version': job['version'], 'resource_ids': selected}


@router.post('/{kind}/{job_id}')
def accept(kind: Literal['development', 'analysis'], job_id: str, data: AcceptanceInput,
           user=Depends(current_user)):
    return accept_resources(kind, job_id, data, user)


init_acceptance()
