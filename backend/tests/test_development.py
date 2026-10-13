"""Business gates and real Git boundaries for governed development tasks."""
import copy
import json
import subprocess

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import core, development, worker
from app.main import app
from conftest import sign_in


def plan_for(ids, paths=None):
    return {'objective': '增加业务能力', 'feasibility': 'feasible', 'questions': [], 'risks': [],
            'projects': [{'project_id': pid, 'impact': '更新服务行为',
                          'allowed_paths': paths or ['value.py', 'tests'],
                          'validation': ['独立运行项目配置的验证命令'], 'rollback': '还原该仓库的变更',
                          'depends_on': [], 'review_tests': False} for pid in ids]}


@pytest.fixture
def environment(tmp_path):
    development.init_development()
    with core.connection() as c:
        c.execute('DELETE FROM development_runs')
    admin = TestClient(app)
    sign_in(admin)
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'value.py').write_text('def value():\n    return 1\n')
    (repo / 'tests').mkdir()
    (repo / 'tests' / 'test_value.py').write_text('from value import value\nassert value() == 2\n')
    (repo / 'pytest.ini').write_text('[pytest]\n')
    for args in (['init'], ['add', '.'], ['-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-m', 'initial']):
        subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
    response = admin.post('/api/projects', json={'name': 'value-service', 'path': str(repo),
                          'checks': ['python3 -c "from value import value; assert value()==2"']})
    assert response.status_code == 200, response.text
    pid = response.json()['id']
    response = admin.post('/api/tasks', json={'project_id': pid, 'title': '更新数值',
                          'requirement': '将value改为2', 'acceptance': 'value返回2'})
    assert response.status_code == 200, response.text
    tid = response.json()['id']
    core.update(tid, status='awaiting_approval', plan='更新value', version=1, plan_schema=1)
    api = FastAPI()
    api.include_router(development.router)
    scoped = TestClient(api)
    scoped.cookies.update(admin.cookies)
    scoped.headers.update(admin.headers)
    return admin, scoped, pid, tid, repo


def test_new_task_requires_complete_structured_plan(environment):
    _, api, pid, tid, _ = environment
    with core.connection() as c:
        with pytest.raises(HTTPException) as exc:
            development.require_approvable(c, core.query('SELECT * FROM tasks WHERE id=?', (tid,), True))
    assert exc.value.status_code == 409
    incomplete = plan_for([pid])
    incomplete['feasibility'] = 'needs_information'
    incomplete['questions'] = ['外部接口是否支持?']
    saved = api.post(f'/api/development/tasks/{tid}/plan', json={'version': 1, 'plan': incomplete})
    assert saved.status_code == 200, saved.text
    response = api.get(f'/api/development/tasks/{tid}/plan').json()
    assert response['version'] == 2 and not response['approvable']
    assert len(response['validation_errors']) == 2
    with core.connection() as c:
        with pytest.raises(HTTPException):
            development.require_approvable(c, c.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())


def test_versioned_save_invalidates_resource_approvals(environment):
    _, api, pid, tid, _ = environment
    from app.orchestration import init_orchestration
    init_orchestration()
    core.execute('INSERT INTO resource_approvals VALUES(?,?,?,?,?,?)',
                 ('development', tid, 1, pid, 'test-admin', 1))
    response = api.post(f'/api/development/tasks/{tid}/plan', json={'version': 1, 'plan': plan_for([pid])})
    assert response.status_code == 200, response.text
    assert core.query('SELECT * FROM resource_approvals WHERE job_id=?', (tid,)) == []
    assert api.post(f'/api/development/tasks/{tid}/plan', json={'version': 1, 'plan': plan_for([pid])}).status_code == 409
    with core.connection() as c:
        assert development.require_approvable(c, c.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())['objective'] == '增加业务能力'


@pytest.mark.parametrize('bad_path', ['../secret', '/absolute', '.', '', 'src/../x', 'src//x', 'src\\x', 'src/*', '.git/config', '.env', 'keys/server.key'])
def test_invalid_scope_never_saved(environment, bad_path):
    _, api, pid, tid, _ = environment
    plan = plan_for([pid])
    plan['projects'][0]['allowed_paths'] = [bad_path]
    response = api.post(f'/api/development/tasks/{tid}/plan', json={'version': 1, 'plan': plan})
    assert response.status_code == 400, response.text
    assert core.query('SELECT version FROM tasks WHERE id=?', (tid,), True)['version'] == 1


def test_project_coverage_and_dependency_cycles():
    valid = plan_for(['a', 'b', 'c'])
    valid['projects'][1]['depends_on'] = ['a']
    valid['projects'][2]['depends_on'] = ['b']
    assert development.project_order(development.validate_plan(valid, ['a', 'b', 'c'])) == ['a', 'b', 'c']
    cyclical = copy.deepcopy(valid)
    cyclical['projects'][0]['depends_on'] = ['c']
    with pytest.raises(ValueError, match='循环'):
        development.validate_plan(cyclical, ['a', 'b', 'c'])
    with pytest.raises(ValueError, match='覆盖'):
        development.validate_plan(plan_for(['a']), ['a', 'b'])
    with pytest.raises(ValueError, match='覆盖'):
        development.validate_plan(plan_for(['a', 'a']), ['a'])


def test_scope_existing_tests_configuration_symlinks_and_generated_changes(environment):
    _, _, pid, _, repo = environment
    base = core.git(repo, 'rev-parse', 'HEAD')
    item = plan_for([pid], ['value.py', 'tests', 'pytest.ini', 'link'])['projects'][0]
    development.validate_change(repo, base, 'value.py', item)
    development.validate_change(repo, base, 'tests/test_new.py', item)
    with pytest.raises(ValueError, match='批准修改范围'):
        development.validate_change(repo, base, 'outside.py', item)
    with pytest.raises(ValueError, match='review_tests'):
        development.validate_change(repo, base, 'tests/test_value.py', item)
    item['review_tests'] = True
    development.validate_change(repo, base, 'tests/test_value.py', item)
    with pytest.raises(ValueError, match='验证或CI配置'):
        development.validate_change(repo, base, 'pytest.ini', item)
    (repo / 'link').symlink_to(repo / 'value.py')
    with pytest.raises(ValueError, match='符号链接'):
        development.validate_change(repo, base, 'link', item)
    (repo / 'link').unlink()
    (repo / 'outside.py').write_text('generated by verification')
    with pytest.raises(ValueError, match='批准修改范围'):
        development.enforce_changes(repo, base, item)


def test_outsider_and_viewer_plan_permissions(environment):
    _, api, pid, tid, _ = environment
    from app.auth import password_hash
    core.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?,?)',
                 ('viewer-user', 'viewer-user', 'Viewer', password_hash('Viewer-password-123'), 'member', 1, 0, 1))
    viewer = TestClient(app)
    sign_in(viewer, 'viewer-user', 'Viewer-password-123')
    scoped = TestClient(api.app)
    scoped.cookies.update(viewer.cookies)
    scoped.headers.update(viewer.headers)
    assert scoped.get(f'/api/development/tasks/{tid}/plan').status_code == 404
    assert scoped.get(f'/api/development/tasks/{tid}/delivery').status_code == 404
    core.execute('INSERT INTO project_members VALUES(?,?,?)', (pid, 'viewer-user', 'viewer'))
    assert scoped.get(f'/api/development/tasks/{tid}/plan').status_code == 200
    assert scoped.post(f'/api/development/tasks/{tid}/plan', json={'version': 1, 'plan': plan_for([pid])}).status_code == 403


def test_transitive_impacts_do_not_cross_hidden_systems(environment):
    _, api, pid, _, _ = environment
    with core.connection() as c:
        for sid in ('first', 'hidden', 'third'):
            c.execute('INSERT INTO enterprise_systems VALUES(?,?,?,?,?)', (sid, sid, '', 'test-admin', 1))
        c.execute('INSERT INTO system_resources VALUES(?,?,?)', ('first', 'project', pid))
        for new_pid, sid in (('p-hidden', 'hidden'), ('p-third', 'third')):
            c.execute('INSERT INTO projects(id,name,path,branch,checks) VALUES(?,?,?,?,?)', (new_pid, new_pid, '/not-used', 'main', '[]'))
            c.execute('INSERT INTO system_resources VALUES(?,?,?)', (sid, 'project', new_pid))
        c.execute('INSERT INTO system_dependencies VALUES(?,?,?)', ('first', 'hidden', 'dependency-one'))
        c.execute('INSERT INTO system_dependencies VALUES(?,?,?)', ('hidden', 'third', 'dependency-two'))
    visible = api.get(f'/api/development/impacts/{pid}')
    assert visible.status_code == 200, visible.text
    assert next(item for item in visible.json() if item['project_id'] == 'p-third')['distance'] == 2
    from app.auth import password_hash
    core.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?,?)',
                 ('impact-user', 'impact-user', 'Impact User', password_hash('Impact-password-123'), 'member', 1, 0, 1))
    for new_pid in (pid, 'p-third'):
        core.execute('INSERT INTO project_members VALUES(?,?,?)', (new_pid, 'impact-user', 'developer'))
    with core.connection() as c:
        user = dict(c.execute("SELECT * FROM users WHERE id='impact-user'").fetchone())
        assert development.visible_impacts(c, user, pid) == []


def test_scripted_execution_obeys_plan_and_keeps_delivery(environment, monkeypatch):
    _, api, pid, tid, _ = environment
    plan = plan_for([pid])
    assert api.post(f'/api/development/tasks/{tid}/plan', json={'version': 1, 'plan': plan}).status_code == 200
    from app.orchestration import ResourceApproval, approve_resources
    with core.connection() as c:
        user = dict(c.execute("SELECT * FROM users WHERE id='test-admin'").fetchone())
    assert approve_resources('development', tid, ResourceApproval(version=2), user)['ready']
    replies = iter([{'tool': 'write_file', 'args': {'path': 'value.py', 'content': 'def value():\n    return 2\n'}},
                    {'done': True, 'summary': '完成'}])
    monkeypatch.setattr(worker, 'model', lambda messages: next(replies))
    worker.run(core.query('SELECT * FROM tasks WHERE id=?', (tid,), True))
    task = core.query('SELECT * FROM tasks WHERE id=?', (tid,), True)
    assert task['status'] == 'review', task
    package = api.get(f'/api/development/tasks/{tid}/delivery').json()['projects'][0]
    assert package['stage'] == 'review' and package['changed_files'] == ['value.py']
    assert package['checks'][0]['exit_code'] == 0
    assert package['base'] and package['handoff']['pr_url'] is None


def test_generated_out_of_scope_file_fails_and_preserves_delivery(environment, monkeypatch):
    _, api, pid, tid, _ = environment
    core.execute('UPDATE projects SET checks=? WHERE id=?',
                 (json.dumps(['python3 -c "open(\'unexpected.txt\',\'w\').write(\'x\')"']), pid))
    plan = plan_for([pid], ['value.py'])
    assert api.post(f'/api/development/tasks/{tid}/plan', json={'version': 1, 'plan': plan}).status_code == 200
    from app.orchestration import ResourceApproval, approve_resources
    with core.connection() as c:
        user = dict(c.execute("SELECT * FROM users WHERE id='test-admin'").fetchone())
    approve_resources('development', tid, ResourceApproval(version=2), user)
    replies = iter([{'tool': 'write_file', 'args': {'path': 'value.py', 'content': 'def value():\n    return 2\n'}},
                    {'done': True, 'summary': '完成'}])
    monkeypatch.setattr(worker, 'model', lambda messages: next(replies))
    worker.run(core.query('SELECT * FROM tasks WHERE id=?', (tid,), True))
    assert core.query('SELECT status FROM tasks WHERE id=?', (tid,), True)['status'] == 'failed'
    package = api.get(f'/api/development/tasks/{tid}/delivery').json()['projects'][0]
    assert package['stage'] == 'failed'
    assert 'unexpected.txt' in package['changed_files'] and '批准修改范围' in package['error']


def test_dependency_verification_gates_writes_and_invalidates_downstream(environment, tmp_path, monkeypatch):
    admin, api, first, tid, _ = environment
    second_repo = tmp_path / 'second'
    second_repo.mkdir()
    (second_repo / 'value.py').write_text('def value():\n    return 1\n')
    for args in (['init'], ['add', '.'], ['-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-m', 'initial']):
        subprocess.run(['git', '-C', str(second_repo), *args], check=True, capture_output=True)
    response = admin.post('/api/projects', json={'name': 'downstream', 'path': str(second_repo),
        'checks': ['python3 -c "import runpy; assert runpy.run_path(\'value.py\')[\'value\']()==2"']})
    assert response.status_code == 200, response.text
    second = response.json()['id']
    core.execute('UPDATE projects SET checks=? WHERE id=?',
        (json.dumps(['python3 -c "import runpy; assert runpy.run_path(\'value.py\')[\'value\']()==2"']), first))
    core.execute('INSERT INTO task_projects VALUES(?,?)', (tid, second))
    plan = plan_for([first, second], ['value.py'])
    plan['projects'][1]['depends_on'] = [first]
    assert api.post(f'/api/development/tasks/{tid}/plan', json={'version': 1, 'plan': plan}).status_code == 200
    from app.orchestration import ResourceApproval, approve_resources
    with core.connection() as c:
        user = dict(c.execute("SELECT * FROM users WHERE id='test-admin'").fetchone())
    assert approve_resources('development', tid, ResourceApproval(version=2), user)['ready']
    def write(pid, value=2):
        return {'tool': 'write_file', 'args': {'project_id': pid, 'path': 'value.py',
            'content': f'def value():\n    return {value}\n'}}
    replies = iter([
        write(second), write(first), write(second),
        {'tool': 'run_check', 'args': {'project_id': first}}, write(second),
        {'tool': 'run_check', 'args': {'project_id': second}}, write(first, 20), write(second, 20),
        write(first), {'tool': 'run_check', 'args': {'project_id': first}}, write(second),
        {'done': True, 'summary': '依赖验证完成'},
    ])
    observed = []
    def scripted(messages):
        last = json.loads(messages[-1]['content'])
        if 'tool_result' in last:
            observed.append(last['tool_result'])
        return next(replies)
    monkeypatch.setattr(worker, 'model', scripted)
    worker.run(core.query('SELECT * FROM tasks WHERE id=?', (tid,), True))
    task = core.query('SELECT * FROM tasks WHERE id=?', (tid,), True)
    assert task['status'] == 'review', task
    rejected = [item for item in observed if isinstance(item, dict) and 'error' in item]
    assert len(rejected) == 3
    assert all('先实现并验证通过依赖项目' in item['error'] for item in rejected)
    package = api.get(f'/api/development/tasks/{tid}/delivery').json()['projects']
    assert len(package) == 2 and all(item['stage'] == 'review' for item in package)


def test_validation_command_drift_requires_new_plan_and_approval(environment):
    _, api, pid, tid, _ = environment
    assert api.post(f'/api/development/tasks/{tid}/plan', json={'version': 1, 'plan': plan_for([pid])}).status_code == 200
    snapshot = json.loads(core.query('SELECT validation_snapshot FROM tasks WHERE id=?', (tid,), True)['validation_snapshot'])
    assert snapshot[pid][0].startswith('python3')
    core.execute('UPDATE projects SET checks=? WHERE id=?', (json.dumps(['python3 -c "pass"']), pid))
    with core.connection() as c:
        with pytest.raises(HTTPException) as error:
            development.require_approvable(c, c.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    assert error.value.status_code == 409 and '验证配置已变化' in error.value.detail
    assert api.post(f'/api/development/tasks/{tid}/plan', json={'version': 2, 'plan': plan_for([pid])}).status_code == 200
    with core.connection() as c:
        assert development.require_approvable(c, c.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())


def test_planning_stores_valid_structured_plan_and_server_validation_snapshot(environment, monkeypatch):
    _, api, pid, tid, _ = environment
    core.update(tid, status='analyzing')
    monkeypatch.setattr(worker, 'model', lambda messages: {'done': True, 'summary': '已调研服务，改value',
                                                          'plan': plan_for([pid])})
    worker.run(core.query('SELECT * FROM tasks WHERE id=?', (tid,), True))
    result = api.get(f'/api/development/tasks/{tid}/plan').json()
    assert result['approvable'] and result['version'] == 2
    assert result['plan']['projects'][0]['allowed_paths'] == ['value.py', 'tests']
    task = core.query('SELECT * FROM tasks WHERE id=?', (tid,), True)
    assert task['status'] == 'awaiting_approval' and task['validation_snapshot']


def test_summary_only_planning_cannot_guess_authorized_scope(environment, monkeypatch):
    _, api, _, tid, _ = environment
    core.update(tid, status='analyzing')
    monkeypatch.setattr(worker, 'model', lambda messages: {'done': True, 'summary': '完成调研'})
    worker.run(core.query('SELECT * FROM tasks WHERE id=?', (tid,), True))
    task = core.query('SELECT * FROM tasks WHERE id=?', (tid,), True)
    assert task['status'] == 'awaiting_approval' and task['structured_plan'] is None
    assert not api.get(f'/api/development/tasks/{tid}/plan').json()['approvable']


def test_explicit_validation_script_is_immutable_even_with_review_tests(environment):
    _, _, pid, _, repo = environment
    base = core.git(repo, 'rev-parse', 'HEAD')
    item = plan_for([pid], ['value.py', 'tests'])['projects'][0]
    item['review_tests'] = True
    item['_validation_paths'] = development.validation_paths(repo, ['python3 tests/test_value.py'])
    assert item['_validation_paths'] == ['tests/test_value.py']
    with pytest.raises(ValueError, match='独立验证文件'):
        development.validate_change(repo, base, 'tests/test_value.py', item)


def test_authorization_failure_cannot_overwrite_cancelled_workflow(environment, monkeypatch):
    _, api, pid, tid, _ = environment
    assert api.post(f'/api/development/tasks/{tid}/plan', json={'version': 1, 'plan': plan_for([pid])}).status_code == 200
    from app import orchestration
    with core.connection() as c:
        user = dict(c.execute("SELECT * FROM users WHERE id='test-admin'").fetchone())
    orchestration.approve_resources('development', tid, orchestration.ResourceApproval(version=2), user)
    expected = []
    def revoke_and_cancel(c, kind, job):
        core.update(tid, status='cancelled')
        expected.extend(core.query('SELECT step_key,status FROM workflow_steps WHERE job_kind=? AND job_id=? ORDER BY position',
                                   ('development', tid)))
        raise HTTPException(403, '审批权限撤销')
    monkeypatch.setattr(orchestration, 'require_execution_approvals', revoke_and_cancel)
    with pytest.raises(InterruptedError):
        worker.active(tid)
    assert core.query('SELECT status FROM tasks WHERE id=?', (tid,), True)['status'] == 'cancelled'
    actual = core.query('SELECT step_key,status FROM workflow_steps WHERE job_kind=? AND job_id=? ORDER BY position', ('development', tid))
    assert actual == expected


def test_verification_content_hash_survives_staging_and_covers_deleted_untracked_files(environment):
    _, _, _, _, repo = environment
    base = core.git(repo, 'rev-parse', 'HEAD')
    (repo / 'value.py').write_text('def value():\n    return 2\n')
    (repo / 'new.txt').write_text('untracked content')
    (repo / 'tests' / 'test_value.py').unlink()
    before = development.working_content_hash(repo, base)
    core.git(repo, 'add', '-A')
    assert development.working_content_hash(repo, base) == before
    (repo / 'new.txt').write_text('changed after staging')
    assert development.working_content_hash(repo, base) != before


def test_first_acceptance_rejects_code_changed_after_worker_verification(environment, monkeypatch):
    admin, api, pid, tid, _ = environment
    assert api.post(f'/api/development/tasks/{tid}/plan', json={'version': 1, 'plan': plan_for([pid])}).status_code == 200
    from app import acceptance, orchestration
    with core.connection() as c:
        user = dict(c.execute("SELECT * FROM users WHERE id='test-admin'").fetchone())
    orchestration.approve_resources('development', tid, orchestration.ResourceApproval(version=2), user)
    replies = iter([{'tool': 'write_file', 'args': {'path': 'value.py', 'content': 'def value():\n    return 2\n'}},
                    {'done': True, 'summary': '完成'}])
    monkeypatch.setattr(worker, 'model', lambda messages: next(replies))
    worker.run(core.query('SELECT * FROM tasks WHERE id=?', (tid,), True))
    task = core.query('SELECT * FROM tasks WHERE id=?', (tid,), True)
    assert task['status'] == 'review'
    result = json.loads(task['result'])
    assert result['verification_hashes'][pid]
    # Modification occurs before the first human acceptance, while stored checks still show success.
    from pathlib import Path
    (Path(task['worktree']) / 'value.py').write_text('def value():\n    return 999\n')
    with pytest.raises(HTTPException) as denied:
        acceptance.accept_resources('development', tid, acceptance.AcceptanceInput(version=2), user)
    assert denied.value.status_code == 409 and '验证后发生变化' in denied.value.detail
    assert core.query('SELECT status FROM tasks WHERE id=?', (tid,), True)['status'] == 'review'


def test_new_task_cannot_accept_missing_verification_hash(environment):
    _, _, pid, tid, _ = environment
    from app import acceptance
    core.update(tid, status='review', result=json.dumps({'summary': 'claimed complete', 'diff': 'change',
                       'checks': [{'project_id': pid, 'command': 'check', 'exit_code': 0, 'output': ''}]}))
    with core.connection() as c:
        user = dict(c.execute("SELECT * FROM users WHERE id='test-admin'").fetchone())
    with pytest.raises(HTTPException) as rejected:
        acceptance.accept_resources('development', tid, acceptance.AcceptanceInput(version=1), user)
    assert rejected.value.status_code == 409 and '缺少代码验证指纹' in rejected.value.detail
