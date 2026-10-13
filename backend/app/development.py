"""Versioned development plans, visible dependency discovery and delivery evidence."""
import json
import hashlib
import shlex
import subprocess
import time
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import audit, current_user, require_project
from .core import connection, git
from .platform import require_system, require_task_projects, task_project_ids

router = APIRouter(prefix='/api/development')
PROTECTED_PARTS = {'.git', '.env', '.venv', 'node_modules'}
TEMP_PARTS = {'__pycache__', '.pytest_cache', 'node_modules', '.venv'}
VALIDATION_FILES = {'pytest.ini', 'conftest.py', 'tox.ini', 'setup.cfg', 'pyproject.toml',
                    'package.json', 'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock',
                    'Makefile', 'Dockerfile', 'compose.yaml', 'docker-compose.yml'}


def init_development():
    with connection() as c:
        names = {row['name'] for row in c.execute('PRAGMA table_info(tasks)')}
        for name, declaration in (('structured_plan', 'TEXT'), ('plan_schema', 'INTEGER NOT NULL DEFAULT 0'),
                                  ('validation_snapshot', 'TEXT')):
            if name not in names:
                c.execute('ALTER TABLE tasks ADD COLUMN ' + name + ' ' + declaration)
        c.execute('''CREATE TABLE IF NOT EXISTS development_runs (
          task_id TEXT NOT NULL, project_id TEXT NOT NULL, stage TEXT NOT NULL,
          base TEXT, checks TEXT NOT NULL DEFAULT '[]', changed_files TEXT NOT NULL DEFAULT '[]',
          error TEXT NOT NULL DEFAULT '', updated REAL NOT NULL,
          PRIMARY KEY(task_id,project_id))''')


class PlanInput(BaseModel):
    version: int = Field(ge=0)
    plan: dict


def _text(value, field, maximum=6000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(field + '必须为非空文本且不能超长')
    return value.strip()


def _texts(value, field, maximum=40):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(field + '必须为有限列表')
    return [_text(item, field, 3000) for item in value]


def relative_path(name):
    """Treat approved prefixes as literal relative paths, never as glob patterns."""
    if not isinstance(name, str) or not name or len(name) > 500:
        raise ValueError('修改路径必须是非空相对路径')
    candidate = name.rstrip('/')
    path = PurePosixPath(candidate)
    if (not candidate or path.is_absolute() or '\\' in candidate or ':' in candidate
            or any(part in ('', '.', '..') for part in candidate.split('/'))
            or any(char in candidate for char in '*?[]\x00\n\r')):
        raise ValueError('修改路径必须是安全的相对文件或目录，不支持通配符')
    if any(part in PROTECTED_PARTS or part.startswith('.env.') or part.endswith(('.pem', '.key'))
           for part in path.parts):
        raise ValueError('不能批准凭证或受保护目录的修改')
    return path.as_posix()


def validate_plan(plan, project_ids):
    if not isinstance(plan, dict):
        raise ValueError('结构化方案必须为对象')
    objective = _text(plan.get('objective'), '目标')
    feasibility = plan.get('feasibility')
    if feasibility not in ('feasible', 'needs_information', 'infeasible'):
        raise ValueError('可行性枚举无效')
    questions = _texts(plan.get('questions', []), '待确认问题')
    risks = _texts(plan.get('risks', []), '风险')
    items = plan.get('projects')
    if not isinstance(items, list) or not items or len(items) > 6:
        raise ValueError('项目方案必须为 1 至 6 项')
    result = []
    for item in items:
        if not isinstance(item, dict) or item.get('project_id') not in project_ids:
            raise ValueError('方案包含未授权项目')
        paths = item.get('allowed_paths')
        if not isinstance(paths, list) or len(paths) > 100:
            raise ValueError('修改范围必须为最多 100 项路径列表')
        paths = list(dict.fromkeys(relative_path(path) for path in paths))
        validation = item.get('validation', [])
        if isinstance(validation, str):
            validation = [validation] if validation.strip() else []
        validation = _texts(validation, '验证方案')
        dependencies = item.get('depends_on', [])
        if (not isinstance(dependencies, list) or len(dependencies) > 6
                or any(not isinstance(pid, str) or pid not in project_ids for pid in dependencies)):
            raise ValueError('项目依赖必须引用本任务中的项目')
        review_tests = item.get('review_tests', False)
        if not isinstance(review_tests, bool):
            raise ValueError('review_tests必须为布尔值')
        result.append({'project_id': item['project_id'], 'impact': _text(item.get('impact'), '影响说明'),
                       'allowed_paths': paths, 'validation': validation,
                       'rollback': _text(item.get('rollback'), '回退方案'),
                       'depends_on': list(dict.fromkeys(dependencies)), 'review_tests': review_tests})
    ids = [item['project_id'] for item in result]
    if len(ids) != len(set(ids)) or set(ids) != set(project_ids):
        raise ValueError('结构化方案必须恰好覆盖全部目标项目')
    normalized = {'objective': objective, 'feasibility': feasibility, 'questions': questions,
                  'risks': risks, 'projects': result}
    project_order(normalized)
    return normalized


def project_order(plan):
    remaining = {item['project_id']: set(item['depends_on']) for item in plan['projects']}
    ordered = []
    while remaining:
        ready = sorted(pid for pid, dependencies in remaining.items() if not dependencies)
        if not ready:
            raise ValueError('项目依赖存在循环或自身依赖')
        ordered.extend(ready)
        for pid in ready:
            del remaining[pid]
        for dependencies in remaining.values():
            dependencies.difference_update(ready)
    return ordered


def approval_errors(plan):
    errors = []
    if plan['feasibility'] != 'feasible':
        errors.append('方案尚未确认可行')
    if plan['questions']:
        errors.append('仍有待确认问题，补全后才能批准')
    for item in plan['projects']:
        if not item['allowed_paths']:
            errors.append(item['project_id'] + '缺少批准修改范围')
        if not item['validation']:
            errors.append(item['project_id'] + '缺少验证方案')
    return errors


def task_plan(c, task):
    raw = dict(task).get('structured_plan')
    if not raw:
        return None
    try:
        return validate_plan(json.loads(raw), task_project_ids(c, task))
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(400, '结构化方案无效：' + str(exc))


def require_approvable(c, task):
    plan = task_plan(c, task)
    if not plan and dict(task).get('plan_schema', 0):
        raise HTTPException(409, '请补全结构化方案和批准修改范围')
    if plan:
        errors = approval_errors(plan)
        if errors:
            raise HTTPException(409, '；'.join(errors))
        frozen = dict(task).get('validation_snapshot')
        if dict(task).get('plan_schema', 0) and not frozen:
            raise HTTPException(409, '方案缺少独立验证配置快照，请重新保存方案')
        if frozen:
            try:
                snapshot = json.loads(frozen)
            except (ValueError, TypeError):
                raise HTTPException(409, '验证配置快照无效，请重新保存方案')
            if snapshot != snapshot_checks(c, task):
                raise HTTPException(409, '项目验证配置已变化，请重新保存并审批方案')
    return plan


def snapshot_checks(c, task):
    snapshots = {}
    for pid in task_project_ids(c, task):
        row = c.execute('SELECT checks FROM projects WHERE id=?', (pid,)).fetchone()
        if not row:
            raise HTTPException(409, '目标项目已经不存在')
        snapshots[pid] = json.loads(row['checks'])
    return snapshots


def _task(c, user, tid, edit=False):
    row = c.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
    if not row:
        raise HTTPException(404, '任务不存在或无权访问')
    projects = require_task_projects(c, user, row)
    if edit and not all(project['my_role'] == 'maintainer' for project in projects):
        if row['created_by'] != user['id'] or any(project['my_role'] not in ('developer', 'maintainer') for project in projects):
            raise HTTPException(403, '只能修改自己创建的任务')
    return dict(row), projects


@router.get('/tasks/{tid}/plan')
def get_plan(tid: str, user=Depends(current_user)):
    with connection() as c:
        task, projects = _task(c, user, tid)
        plan = task_plan(c, task)
        errors = approval_errors(plan) if plan else ['尚未补全结构化方案和批准修改范围']
        if plan and not errors:
            try:
                require_approvable(c, task)
            except HTTPException as exc:
                errors.append(str(exc.detail))
    return {'version': task['version'], 'plan': plan, 'approvable': not errors,
            'validation_errors': errors, 'warnings': errors,
            'projects': [{'project_id': project['id'], 'project_name': project['name'],
                          'checks': json.loads(project['checks'])} for project in projects]}


@router.post('/tasks/{tid}/plan')
def save_plan(tid: str, data: PlanInput, user=Depends(current_user)):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        task, _ = _task(c, user, tid, True)
        try:
            plan = validate_plan(data.plan, task_project_ids(c, task))
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(400, str(exc))
        frozen = json.dumps(snapshot_checks(c, task), ensure_ascii=False)
        saved = c.execute('''UPDATE tasks SET structured_plan=?,validation_snapshot=?,plan_schema=1,version=version+1,
          approved=NULL,approved_by=NULL WHERE id=? AND status='awaiting_approval' AND version=?''',
                          (json.dumps(plan, ensure_ascii=False), frozen, tid, data.version))
        if saved.rowcount != 1:
            raise HTTPException(409, '状态或方案版本已变化，请刷新')
        from .orchestration import invalidate_approvals
        invalidate_approvals(c, 'development', tid)
        audit(c, user['id'], 'structured_plan_updated', tid, str(data.version + 1))
    return {'ok': True, 'version': data.version + 1}


@router.get('/impacts/{project_id}')
def impacts(project_id: str, user=Depends(current_user)):
    with connection() as c:
        return visible_impacts(c, user, project_id)


def visible_impacts(c, user, project_id):
    """Traverse visible dependency edges in both directions without crossing hidden nodes."""
    require_project(c, user, project_id, 'developer')
    origin = c.execute("SELECT system_id FROM system_resources WHERE kind='project' AND resource_id=?", (project_id,)).fetchone()
    if not origin:
        return []
    first = require_system(c, user, origin['system_id'])
    queue = [(first, [first['name']], [], 0)]
    visited = {first['id']}
    found = {}
    while queue:
        system, chain, descriptions, distance = queue.pop(0)
        for link in c.execute("SELECT resource_id FROM system_resources WHERE kind='project' AND system_id=?", (system['id'],)).fetchall():
            try:
                project = require_project(c, user, link['resource_id'], 'developer')
            except HTTPException:
                continue
            if project['id'] != project_id:
                found[project['id']] = {'project_id': project['id'], 'project_name': project['name'],
                    'system_id': system['id'], 'system_name': system['name'], 'distance': distance,
                    'chain': chain, 'relationship': ' → '.join(descriptions) or '同一企业系统'}
        edges = c.execute('SELECT * FROM system_dependencies WHERE source_id=? OR target_id=?', (system['id'], system['id'])).fetchall()
        for edge in edges:
            target = edge['target_id'] if edge['source_id'] == system['id'] else edge['source_id']
            if target in visited:
                continue
            visited.add(target)
            try:
                following = require_system(c, user, target)
            except HTTPException:
                continue
            queue.append((following, chain + [following['name']], descriptions + [edge['description']], distance + 1))
    return sorted(found.values(), key=lambda item: (item['distance'], item['project_name']))


def checkpoint(tid, pid, stage, **values):
    if stage not in ('pending', 'research', 'planned', 'execution', 'verification', 'review', 'failed'):
        raise ValueError('检查点阶段无效')
    fields = {'stage': stage, 'updated': time.time(), **values}
    for field in ('checks', 'changed_files'):
        if field in fields:
            fields[field] = json.dumps(fields[field], ensure_ascii=False)
    if set(fields) - {'stage', 'updated', 'base', 'checks', 'changed_files', 'error'}:
        raise ValueError('检查点字段无效')
    with connection() as c:
        c.execute('''INSERT OR IGNORE INTO development_runs(task_id,project_id,stage,updated)
          VALUES(?,?,?,?)''', (tid, pid, stage, time.time()))
        c.execute('UPDATE development_runs SET ' + ','.join(key + '=?' for key in fields) +
                  ' WHERE task_id=? AND project_id=?', (*fields.values(), tid, pid))


def _existing(root, base, path):
    try:
        git(root, 'cat-file', '-e', base + ':' + path)
        return True
    except Exception:
        return False


def _test_file(path):
    parts = PurePosixPath(path).parts
    filename = parts[-1].lower()
    return (any(part.lower() in ('test', 'tests', '__tests__', 'spec', 'specs') for part in parts)
            or filename.startswith('test_') or filename.endswith(('_test.py', '.test.ts', '.spec.ts', '.test.js', '.spec.js')))


def validate_change(root, base, path, project_plan):
    name = relative_path(path)
    current = Path(root).resolve()
    for part in PurePosixPath(name).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('不能通过符号链接修改文件：' + name)
    resolved = (Path(root) / name).resolve()
    if Path(root).resolve() not in resolved.parents:
        raise ValueError('修改路径超出工作区或指向符号链接')
    if not any(name == prefix or name.startswith(prefix + '/') for prefix in project_plan['allowed_paths']):
        raise ValueError('文件不在批准修改范围：' + name)
    if name in project_plan.get('_validation_paths', []):
        raise ValueError('不能修改项目配置指定的独立验证文件：' + name)
    parts = PurePosixPath(name).parts
    # Validation commands belong to the administrator configuration. Never let the agent weaken them.
    if (any(part in ('.github', '.gitlab', '.circleci') for part in parts)
            or parts[-1] in VALIDATION_FILES or parts[-1].startswith(('jest.config', 'vitest.config', 'vite.config', 'webpack.config'))):
        if _existing(root, base, name):
            raise ValueError('不能修改既有验证或CI配置：' + name)
    if _test_file(name) and _existing(root, base, name) and not project_plan.get('review_tests'):
        raise ValueError('修改既有测试需要方案明确 review_tests：' + name)
    return name


def validation_paths(root, commands):
    """Freeze explicit repository scripts/config files referenced by admin-owned checks."""
    found = set()
    root = Path(root).resolve()
    for command in commands:
        try:
            tokens = shlex.split(command)
        except ValueError:
            continue
        candidates = []
        for index, token in enumerate(tokens):
            candidates.append(token.split('=', 1)[-1] if token.startswith('--') else token)
            if index and tokens[index - 1] == '-m':
                candidates.append(token.replace('.', '/') + '.py')
                candidates.append(token.replace('.', '/') + '/__main__.py')
        for name in candidates:
            if not name or name.startswith('-'):
                continue
            path = (root / name).resolve()
            if root in path.parents and path.is_file():
                found.add(path.relative_to(root).as_posix())
    return sorted(found)


def changed_files(root, base='HEAD'):
    def names(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True,
                                       stderr=subprocess.STDOUT, timeout=30).split('\x00')
    tracked = names('diff', '--name-only', '-z', base)
    untracked = names('ls-files', '--others', '-z')
    # Even tracked cache files remain subject to scope checks. Ignore only untracked runtime caches.
    return sorted({name for name in tracked if name} | {
        name for name in untracked if name and not any(part in TEMP_PARTS for part in PurePosixPath(name).parts)})


def enforce_changes(root, base, project_plan):
    changes = changed_files(root, base)
    for name in changes:
        validate_change(root, base, name, project_plan)
    return changes


def working_content_hash(root, base):
    """Content identity relative to the immutable base, independent of staging state."""
    root = Path(root).resolve()
    digest = hashlib.sha256()
    for name in changed_files(root, base):
        digest.update(name.encode())
        digest.update(b'\x00')
        path = root / name
        if path.is_symlink():
            # Do not follow a newly introduced symlink into another repository or credential.
            digest.update(b'<symlink>' + str(path.readlink()).encode())
        elif path.is_file():
            if root not in path.resolve().parents:
                raise ValueError('交付文件路径超出工作区')
            digest.update(b'<file>')
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(65536), b''):
                    digest.update(chunk)
        else:
            digest.update(b'<deleted>')
        digest.update(b'\x00')
    return digest.hexdigest()


@router.get('/tasks/{tid}/delivery')
def delivery(tid: str, user=Depends(current_user)):
    with connection() as c:
        task, projects = _task(c, user, tid)
        runs = {row['project_id']: dict(row) for row in c.execute('SELECT * FROM development_runs WHERE task_id=?', (tid,))}
        worktrees = {row['project_id']: dict(row) for row in c.execute('SELECT * FROM task_project_runs WHERE task_id=?', (tid,))}
    result = []
    for project in projects:
        pid = project['id']
        run = runs.get(pid, {})
        worktree = worktrees.get(pid, {})
        snapshot = json.loads(task['validation_snapshot']) if task.get('validation_snapshot') else {}
        result.append({'project_id': pid, 'project_name': project['name'], 'approved_checks': snapshot.get(pid, []),
            'base': run.get('base') or worktree.get('base'), 'stage': run.get('stage', 'pending'),
            'changed_files': json.loads(run.get('changed_files', '[]')), 'checks': json.loads(run.get('checks', '[]')),
            'error': run.get('error', ''), 'updated': run.get('updated'),
            'handoff': {'branch': ('esi/' + tid + (('-' + pid) if pid != task['project_id'] else '')) if worktree else None,
                        'pr_url': None, 'ci_url': None,
                        'instruction': '在独立工作区审查差异，人工创建 PR 并运行 CI；平台未自动合并或发布'}})
    return {'task_id': tid, 'status': task['status'], 'version': task['version'], 'projects': result}


init_development()
