"""Per-resource review without granting cross-system data access."""
import json
import time
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import acceptance, core
from app.auth import password_hash
from app.main import app
from conftest import sign_in


@pytest.fixture
def reviewers():
    acceptance.init_acceptance()
    with core.connection() as c:
        c.execute('DELETE FROM resource_acceptances')
        for uid in ('left-owner', 'right-owner', 'outsider'):
            c.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?,?)',
                (uid, uid, uid, password_hash('Owner-password-123'), 'member', 1, 0, time.time()))
    isolated = FastAPI()
    isolated.include_router(acceptance.router)
    clients = {}
    for uid in ('left-owner', 'right-owner', 'outsider'):
        main_client = TestClient(app)
        sign_in(main_client, uid, 'Owner-password-123')
        client = TestClient(isolated)
        client.cookies.update(main_client.cookies)
        client.headers.update(main_client.headers)
        clients[uid] = client
    return clients


def job_for(kind):
    ids = ['left-resource', 'right-resource']
    jid = 'acceptance-job-' + kind + '-' + uuid.uuid4().hex[:8]
    with core.connection() as c:
        if kind == 'development':
            for rid in ids:
                c.execute('INSERT INTO projects(id,name,path,branch,checks) VALUES(?,?,?,?,?)',
                    (rid, rid, '/fixture-not-used', 'main', '["python3 -c pass"]'))
            for rid, actor in zip(ids, ('left-owner', 'right-owner')):
                c.execute('INSERT INTO project_members VALUES(?,?,?)', (rid, actor, 'maintainer'))
            result = {'summary': 'SECRET OVERALL SUMMARY', 'diff': 'SECRET COMBINED DIFF', 'checks': [
                {'project_id': rid, 'command': 'validate-' + rid, 'exit_code': 0, 'output': rid + '-own-check'}
                for rid in ids]}
            c.execute('''INSERT INTO tasks(id,project_id,title,requirement,acceptance,status,version,result,created_by,created)
                VALUES(?,?,?,?,?,?,?,?,?,?)''', (jid, ids[0], 'Cross-system task', 'requirement', 'criteria',
                'review', 3, json.dumps(result), 'test-admin', time.time()))
            c.executemany('INSERT INTO task_projects VALUES(?,?)', [(jid, rid) for rid in ids])
        else:
            for rid in ids:
                c.execute('INSERT INTO data_sources(id,name,kind,path,created_by,created) VALUES(?,?,?,?,?,?)',
                    (rid, rid, 'csv', '/fixture-not-used.csv', 'test-admin', time.time()))
            for rid, actor in zip(ids, ('left-owner', 'right-owner')):
                c.execute('INSERT INTO data_source_members VALUES(?,?,?)', (rid, actor, 'owner'))
            result = {'report': 'SECRET WHOLE REPORT', 'claim_validation': 'references_validated_content_requires_review',
                'rows': [{'source_id': rid, 'source_name': rid, 'metric_id': rid + '-metric',
                          'value': 10 if rid == ids[0] else 987654} for rid in ids],
                'evidence': {'sources': [{'source_id': rid, 'source_name': rid, 'complete': True} for rid in ids],
                    'metrics': [{'id': rid + '-metric', 'source_id': rid, 'name': rid + '-metric'} for rid in ids],
                    'quality': {rid + '-metric': {'scanned_rows': 2} for rid in ids},
                    'calculations': [{'value': 'SECRET CROSS SOURCE COMPARISON'}], 'limitations': ['not transactional']}}
            c.execute('''INSERT INTO analysis_jobs(id,source_id,title,question,report_kind,status,version,result,created_by,created)
                VALUES(?,?,?,?,?,?,?,?,?,?)''', (jid, ids[0], 'Cross-source report', 'question', 'strategy',
                'review', 3, json.dumps(result), 'test-admin', time.time()))
            c.executemany('INSERT INTO analysis_job_sources VALUES(?,?)', [(jid, rid) for rid in ids])
    return jid, ids, result


def send(client, kind, jid, rid, version=3):
    return client.post(f'/api/acceptance/{kind}/{jid}', json={'version': version, 'resource_ids': [rid]})


@pytest.mark.parametrize('kind', ['development', 'analysis'])
def test_split_review_is_private_and_completes_only_when_all_accept(reviewers, kind):
    jid, ids, _ = job_for(kind)
    left, right = reviewers['left-owner'], reviewers['right-owner']
    left_items = left.get('/api/acceptance/inbox').json()['items']
    assert len(left_items) == 1 and left_items[0]['resource_id'] == ids[0]
    serialized = json.dumps(left_items)
    assert ids[1] not in serialized and 'SECRET' not in serialized and '987654' not in serialized
    assert reviewers['outsider'].get('/api/acceptance/inbox').json() == {'items': []}
    first = send(left, kind, jid, ids[0])
    assert first.status_code == 200, first.text
    assert first.json()['completed'] is False
    assert left.get('/api/acceptance/inbox').json()['items'] == []
    table = 'tasks' if kind == 'development' else 'analysis_jobs'
    assert core.query('SELECT status FROM ' + table + ' WHERE id=?', (jid,), True)['status'] == 'review'
    last = send(right, kind, jid, ids[1])
    assert last.status_code == 200 and last.json()['completed'] is True, last.text
    job = core.query('SELECT status,completed_by FROM ' + table + ' WHERE id=?', (jid,), True)
    assert job == {'status': 'completed', 'completed_by': 'right-owner'}
    assert send(right, kind, jid, ids[1]).status_code == 409


@pytest.mark.parametrize('kind', ['development', 'analysis'])
def test_no_access_or_readonly_cannot_accept_or_see_other_result(reviewers, kind):
    jid, ids, _ = job_for(kind)
    assert send(reviewers['outsider'], kind, jid, ids[0]).status_code == 404
    assert send(reviewers['left-owner'], kind, jid, ids[1]).status_code == 404
    memberships = 'project_members' if kind == 'development' else 'data_source_members'
    core.execute('INSERT INTO ' + memberships + ' VALUES(?,?,?)', (ids[0], 'outsider', 'viewer'))
    assert send(reviewers['outsider'], kind, jid, ids[0]).status_code == 403
    assert reviewers['outsider'].get('/api/acceptance/inbox').json()['items'] == []
    assert reviewers['outsider'].post(f'/api/acceptance/{kind}/{jid}', json={'version': 3}).status_code == 403


@pytest.mark.parametrize('kind', ['development', 'analysis'])
def test_version_and_changed_result_do_not_reuse_acceptances(reviewers, kind):
    jid, ids, result = job_for(kind)
    assert send(reviewers['left-owner'], kind, jid, ids[0], 2).status_code == 409
    assert send(reviewers['left-owner'], kind, jid, ids[0]).json()['completed'] is False
    table = 'tasks' if kind == 'development' else 'analysis_jobs'
    if kind == 'development':
        result['checks'][0]['output'] = 'updated-validation-evidence'
    else:
        result['rows'][0]['value'] = 20
    core.execute('UPDATE ' + table + ' SET result=? WHERE id=?', (json.dumps(result), jid))
    assert len(reviewers['left-owner'].get('/api/acceptance/inbox').json()['items']) == 1
    assert send(reviewers['right-owner'], kind, jid, ids[1]).json()['completed'] is False
    core.execute('UPDATE ' + table + ' SET version=4 WHERE id=?', (jid,))
    assert send(reviewers['left-owner'], kind, jid, ids[0]).status_code == 409
    assert send(reviewers['left-owner'], kind, jid, ids[0], 4).json()['completed'] is False
    assert send(reviewers['right-owner'], kind, jid, ids[1], 4).json()['completed'] is True


@pytest.mark.parametrize('kind', ['development', 'analysis'])
def test_revoked_reviewer_prevents_completion_until_current_owner_accepts(reviewers, kind):
    jid, ids, _ = job_for(kind)
    assert send(reviewers['left-owner'], kind, jid, ids[0]).json()['completed'] is False
    table, key = ('project_members', 'project_id') if kind == 'development' else ('data_source_members', 'source_id')
    core.execute('DELETE FROM ' + table + ' WHERE ' + key + '=? AND user_id=?', (ids[0], 'left-owner'))
    assert send(reviewers['right-owner'], kind, jid, ids[1]).json()['completed'] is False
    role = 'maintainer' if kind == 'development' else 'owner'
    core.execute('INSERT INTO ' + table + ' VALUES(?,?,?)', (ids[0], 'right-owner', role))
    assert send(reviewers['right-owner'], kind, jid, ids[0]).json()['completed'] is True


@pytest.mark.parametrize('kind', ['development', 'analysis'])
def test_missing_result_and_model_pending_are_not_reviewable(reviewers, kind):
    jid, ids, result = job_for(kind)
    table = 'tasks' if kind == 'development' else 'analysis_jobs'
    core.execute('UPDATE ' + table + ' SET result=NULL WHERE id=?', (jid,))
    assert send(reviewers['left-owner'], kind, jid, ids[0]).status_code == 409
    assert reviewers['left-owner'].get('/api/acceptance/inbox').json()['items'] == []
    result['claim_validation'] = 'model_pending'
    core.execute('UPDATE ' + table + ' SET result=? WHERE id=?', (json.dumps(result), jid))
    assert send(reviewers['left-owner'], kind, jid, ids[0]).status_code == 409


def test_all_resources_legacy_wrapper_requires_all_authority(reviewers):
    jid, ids, _ = job_for('development')
    from fastapi import HTTPException
    with core.connection() as c:
        left = dict(c.execute("SELECT * FROM users WHERE id='left-owner'").fetchone())
        admin = dict(c.execute("SELECT * FROM users WHERE id='test-admin'").fetchone())
    with pytest.raises(HTTPException) as denied:
        acceptance.accept_resources('development', jid, acceptance.AcceptanceInput(version=3), left, require_all=True)
    assert denied.value.status_code == 404
    result = acceptance.accept_resources('development', jid, acceptance.AcceptanceInput(version=3), admin, require_all=True)
    assert result['completed'] and result['resource_ids'] == ids


def test_real_worktree_hash_covers_truncated_tail_and_untracked_content(reviewers, tmp_path):
    import subprocess
    jid, ids, _ = job_for('development')
    trees = []
    for index, rid in enumerate(ids):
        root = tmp_path / rid
        root.mkdir()
        (root / 'code.txt').write_text('original\n')
        for args in (['init'], ['add', '.'], ['-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-m', 'initial']):
            subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)
        base = core.git(root, 'rev-parse', 'HEAD')
        if index == 0:
            (root / 'code.txt').write_text('OWN-LEFT-LINE\n' * 20000 + 'original-tail\n')
            (root / 'new.txt').write_text('untracked-original')
        else:
            (root / 'code.txt').write_text('SECRET-RIGHT-CODE\n')
        core.execute('INSERT INTO task_project_runs VALUES(?,?,?,?)', (jid, rid, base, str(root)))
        trees.append(root)
    left, right = reviewers['left-owner'], reviewers['right-owner']
    item = left.get('/api/acceptance/inbox').json()['items'][0]
    assert item['scoped_result']['diff_truncated']
    assert 'SECRET-RIGHT' not in json.dumps(item)
    assert send(left, 'development', jid, ids[0]).json()['completed'] is False
    (trees[0] / 'new.txt').write_text('untracked-changed')
    assert len(left.get('/api/acceptance/inbox').json()['items']) == 1
    assert send(left, 'development', jid, ids[0]).json()['completed'] is False
    # The first displayed 100000 characters stay identical, but the undisplayed tail changed.
    (trees[0] / 'code.txt').write_text('OWN-LEFT-LINE\n' * 20000 + 'changed-tail\n')
    assert send(right, 'development', jid, ids[1]).json()['completed'] is False
    assert len(left.get('/api/acceptance/inbox').json()['items']) == 1
    assert send(left, 'development', jid, ids[0]).json()['completed'] is True
