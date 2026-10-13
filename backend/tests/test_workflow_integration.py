"""Real API integration for approval, employee scope, measurements and dispatch."""
import json
import subprocess
import time

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import core, http_connector, main, orchestration
from conftest import sign_in


@pytest.fixture
def admin():
    client = TestClient(main.app)
    sign_in(client)
    return client


def member(admin, name):
    initial = name+'-initial-password-123'
    result = admin.post('/api/users', json={'username': name, 'display_name': name,
                        'password': initial, 'role': 'member'})
    assert result.status_code == 200, result.text
    client = TestClient(main.app)
    sign_in(client, name, initial)
    changed = client.post('/api/auth/password', json={'current_password': initial,
                          'new_password': name+'-changed-password-456'})
    client.headers['X-ESI-CSRF'] = changed.json()['csrf']
    return client, result.json()['id']


def project(admin, tmp_path, name='service'):
    repo = tmp_path/name
    repo.mkdir()
    (repo/'service.py').write_text('VALUE=1\n')
    subprocess.run(['git','init',str(repo)], check=True, capture_output=True)
    subprocess.run(['git','-C',str(repo),'add','.'], check=True, capture_output=True)
    subprocess.run(['git','-C',str(repo),'-c','user.name=Test','-c','user.email=test@localhost',
                    'commit','-m','initial'], check=True, capture_output=True)
    result = admin.post('/api/projects', json={'name': name, 'path': str(repo), 'checks': ['python3 -c "pass"']})
    assert result.status_code == 200, result.text
    return result.json()['id']


def development(admin, ids):
    result = admin.post('/api/tasks', json={'project_id': ids[0], 'project_ids': ids,
                        'title': 'Change services', 'requirement': 'Update services', 'acceptance': 'Checks pass'})
    assert result.status_code == 200, result.text
    tid = result.json()['id']
    core.update(tid, status='awaiting_approval', plan='Explicit implementation', version=1)
    plan = {'objective': 'Update services', 'feasibility': 'feasible', 'questions': [], 'risks': [],
            'projects': [{'project_id': pid, 'impact': 'Update service contract', 'allowed_paths': ['service.py'],
                          'validation': ['Run independent checks'], 'rollback': 'Restore baseline', 'depends_on': []} for pid in ids]}
    result = admin.post('/api/development/tasks/'+tid+'/plan', json={'version': 1, 'plan': plan})
    assert result.status_code == 200, result.text
    return tid, plan


def analysis_action(admin, tmp_path, monkeypatch):
    monkeypatch.setenv('ESI_DATA_ROOTS', str(tmp_path))
    path = tmp_path/'orders.csv'
    path.write_text('amount\n10\n20\n')
    source = admin.post('/api/data-sources', json={'name': 'Orders', 'kind': 'csv', 'path': str(path)})
    assert source.status_code == 200, source.text
    sid = source.json()['id']
    metric = admin.post('/api/data-sources/'+sid+'/metrics', json={'name': 'Revenue', 'table_name': 'data',
                       'aggregation': 'sum', 'value_column': 'amount', 'dimensions': []})
    assert metric.status_code == 200, metric.text
    mid = metric.json()['id']
    job = admin.post('/api/analysis-jobs', json={'source_id': sid, 'title': 'Revenue strategy',
                     'question': 'Improve revenue', 'report_kind': 'strategy'})
    jid = job.json()['id']
    rid = 'action-example'
    with core.connection() as c:
        c.execute("UPDATE analysis_jobs SET status='review' WHERE id=?", (jid,))
        c.execute('''INSERT INTO recommendations(id,job_id,title,action,success_metric,created,updated)
                     VALUES(?,?,?,?,?,?,?)''',
                  (rid,jid,'Follow up','Contact customers','Revenue',time.time(),time.time()))
    return sid, mid, jid, rid, path


def test_split_approval_is_private_versioned_and_complete(admin, tmp_path):
    first = project(admin,tmp_path,'first'); second = project(admin,tmp_path,'second')
    alice, alice_id = member(admin,'alice'); bob, bob_id = member(admin,'bob')
    for pid, uid in ((first,alice_id),(second,bob_id)):
        admin.post('/api/projects/'+pid+'/members',json={'user_id':uid,'role':'maintainer'})
    tid, plan = development(admin,[first,second])
    assert alice.get('/api/tasks/'+tid).status_code == 404
    inbox = alice.get('/api/orchestration/inbox').json()['items']
    assert len(inbox) == 1 and inbox[0]['resource_id'] == first
    assert inbox[0]['scoped_plan']['allowed_paths'] == ['service.py']
    assert alice.post('/api/orchestration/development/'+tid+'/approve',
                      json={'version':2,'resource_ids':[second]}).status_code == 404
    result = alice.post('/api/orchestration/development/'+tid+'/approve',
                        json={'version':2,'resource_ids':[first]})
    assert result.status_code == 200 and result.json()['ready'] is False
    assert core.query('SELECT status FROM tasks WHERE id=?',(tid,),True)['status'] == 'awaiting_approval'
    result = bob.post('/api/orchestration/development/'+tid+'/approve',json={'version':2,'resource_ids':[second]})
    assert result.status_code == 200 and result.json()['ready'] is True
    claimed = orchestration.claim_next()
    assert claimed[0] == 'development' and claimed[1]['status'] == 'running'
    assert orchestration.claim_next() is None
    admin.post('/api/projects/'+first+'/members',json={'user_id':alice_id,'role':'viewer'})
    with core.connection() as c:
        job = core.query('SELECT * FROM tasks WHERE id=?',(tid,),True)
        with pytest.raises(HTTPException): orchestration.require_execution_approvals(c,'development',job)


def test_changed_plan_clears_partial_approvals_and_retry_requires_reapproval(admin,tmp_path):
    pid = project(admin,tmp_path)
    tid, plan = development(admin,[pid])
    approved = admin.post('/api/orchestration/development/'+tid+'/approve',json={'version':2,'resource_ids':[pid]})
    assert approved.status_code == 200
    core.update(tid,status='failed')
    assert admin.post('/api/orchestration/development/'+tid+'/retry',json={}).status_code == 200
    job = core.query('SELECT * FROM tasks WHERE id=?',(tid,),True)
    assert job['status'] == 'awaiting_approval' and job['approved'] is None and job['version'] == 3
    assert core.query('SELECT * FROM resource_approvals WHERE job_id=?',(tid,)) == []
    history=admin.get('/api/orchestration/development/'+tid+'/history').json()
    assert len(history)==1 and history[0]['version']==2 and history[0]['complete']==1
    assert history[0]['payload']['structured_plan']['objective']=='Update services'
    assert admin.post('/api/orchestration/development/'+tid+'/approve',json={'version':2,'resource_ids':[pid]}).status_code == 409
    assert admin.post('/api/development/tasks/'+tid+'/plan',json={'version':3,'plan':plan}).status_code == 200


def test_cancelled_pending_analysis_is_never_claimed(admin,tmp_path,monkeypatch):
    sid,mid,jid,rid,path = analysis_action(admin,tmp_path,monkeypatch)
    with core.connection() as c:
        c.execute("UPDATE analysis_jobs SET status='pending' WHERE id=?",(jid,))
    assert admin.post('/api/analysis-jobs/'+jid+'/cancel',json={}).status_code == 200
    assert orchestration.claim_next() is None
    assert core.query('SELECT status FROM analysis_jobs WHERE id=?',(jid,),True)['status'] == 'cancelled'


def test_employee_action_does_not_grant_data_access_and_retains_history(admin,tmp_path,monkeypatch):
    sid,mid,jid,rid,path = analysis_action(admin,tmp_path,monkeypatch)
    employee, uid = member(admin,'employee'); stranger,_ = member(admin,'stranger')
    result = admin.post('/api/actions/'+rid+'/configure',json={'assignee_id':uid,'due_at':time.time()-60})
    assert result.status_code == 200,result.text
    assert employee.get('/api/analysis-jobs/'+jid).status_code == 404
    assert employee.get('/api/data-sources').json() == []
    inbox = employee.get('/api/actions/inbox').json()
    assert len(inbox['items']) == 1 and inbox['items'][0]['overdue'] is True
    assert inbox['items'][0]['can_measure'] is False
    assert employee.post('/api/actions/'+rid+'/measure',json={}).status_code == 403
    assert stranger.post('/api/actions/'+rid+'/feedback',json={'status':'done','outcome':'x'}).status_code == 404
    assert employee.post('/api/actions/'+rid+'/feedback',json={'status':'in_progress','outcome':'Started'}).status_code == 200
    assert employee.post('/api/actions/'+rid+'/feedback',json={'status':'done','outcome':'Contacted customers'}).status_code == 200
    item = employee.get('/api/actions/inbox').json()['items'][0]
    assert [r['detail'].get('outcome') for r in item['history'] if r['action']=='feedback'] == ['Started','Contacted customers']
    nid = inbox['notifications'][0]['id']
    assert stranger.post('/api/actions/notifications/'+nid+'/read',json={}).status_code == 404
    assert employee.post('/api/actions/notifications/'+nid+'/read',json={}).status_code == 200


def test_measurement_and_idempotent_development_handoff(admin,tmp_path,monkeypatch):
    sid,mid,jid,rid,path = analysis_action(admin,tmp_path,monkeypatch)
    configured = admin.post('/api/actions/'+rid+'/configure',json={'metric_id':mid,'baseline':20,'target':30})
    assert configured.status_code == 200,configured.text
    result = admin.post('/api/actions/'+rid+'/measure',json={})
    assert result.status_code == 200,result.text
    assert result.json()['value'] == 30 and result.json()['change'] == 10 and result.json()['target_met'] is True
    path.write_text('amount\n10\n20\n5\n')
    assert admin.post('/api/actions/'+rid+'/measure',json={}).json()['value'] == 35
    pid = project(admin,tmp_path)
    payload = {'project_ids':[pid],'title':'Implement recommendation','requirement':'Add follow-up flow','acceptance':'Independent checks pass'}
    handoff = admin.post('/api/actions/'+rid+'/development',json=payload)
    assert handoff.status_code == 200,handoff.text
    repeated = admin.post('/api/actions/'+rid+'/development',json=payload)
    assert repeated.json()['id'] == handoff.json()['id'] and repeated.json()['existing'] is True
    task = core.query('SELECT * FROM tasks WHERE id=?',(handoff.json()['id'],),True)
    assert task['plan_schema'] == 1 and task['status'] == 'pending' and task['approved'] is None
    assert rid in core.query('SELECT target FROM audit WHERE action=?',('action_development_created',),True)['target']


def test_external_metric_validation_does_not_hold_database_write_lock(admin,tmp_path,monkeypatch):
    sid,mid,jid,rid,path = analysis_action(admin,tmp_path,monkeypatch)
    original = main.validate_metric
    def validate_without_lock(source,metric):
        with core.connection() as c:
            c.execute('PRAGMA busy_timeout=100')
            c.execute('BEGIN IMMEDIATE')
        return original(source,metric)
    monkeypatch.setattr(main,'validate_metric',validate_without_lock)
    result = admin.post('/api/data-sources/'+sid+'/metrics',json={'name':'Count','table_name':'data','aggregation':'count'})
    assert result.status_code == 200,result.text


def test_https_pagination_retries_and_rejects_incomplete_or_cross_origin(monkeypatch):
    monkeypatch.setenv('ESI_CONNECTOR_HOSTS','api.example,other.example')
    attempts=[]
    def handler(request):
        attempts.append(str(request.url))
        if len(attempts)==1: return httpx.Response(503)
        if request.url.path=='/first': return httpx.Response(200,json={'data':[{'id':1}],'next':'/second'})
        return httpx.Response(200,json={'data':[{'id':2}],'next':None})
    original=httpx.Client
    monkeypatch.setattr(http_connector.httpx,'Client',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    result=http_connector.records('https://api.example/first','',{'pagination':'next_url','max_pages':3,'retries':1})
    assert result==[{'id':1},{'id':2}] and len(attempts)==3
    attempts.clear()
    with pytest.raises(ValueError,match='启用分页'):
        http_connector.records('https://api.example/first','',{'retries':1})
    attempts.clear()
    with pytest.raises(ValueError,match='分页上限'):
        http_connector.records('https://api.example/first','',{'pagination':'next_url','max_pages':1,'retries':1})
    monkeypatch.setattr(http_connector.httpx,'Client',lambda **kwargs:original(transport=httpx.MockTransport(
        lambda request:httpx.Response(200,json={'data':[{'id':1}],'next':'https://other.example/data'})),**kwargs))
    with pytest.raises(ValueError,match='同源'):
        http_connector.records('https://api.example/first','',{'pagination':'next_url'})


def test_employee_measurement_history_hides_source_metadata(admin,tmp_path,monkeypatch):
    sid,mid,jid,rid,path=analysis_action(admin,tmp_path,monkeypatch)
    employee,uid=member(admin,'recipient')
    assert admin.post('/api/actions/'+rid+'/configure',json={'assignee_id':uid,'metric_id':mid,'baseline':20,'target':30}).status_code==200
    assert admin.post('/api/actions/'+rid+'/measure',json={}).status_code==200
    item=employee.get('/api/actions/inbox').json()['items'][0]
    assert item['measurement']['value']==30
    assert 'evidence' not in item['measurement'] and 'metric_id' not in item and 'dimensions' not in item
    assert all('evidence' not in row['detail'] for row in item['history'])
    encoded=json.dumps(item)
    assert 'table_name' not in encoded and 'value_column' not in encoded and 'source_id' not in encoded
    assert admin.get('/api/platform/analysis-jobs/'+jid+'/recommendations').json()[0]['measurement']['evidence']['metrics']


def test_measurement_rejects_term_and_midflight_source_drift(admin,tmp_path,monkeypatch):
    from app import analytics
    sid,mid,jid,rid,path=analysis_action(admin,tmp_path,monkeypatch)
    payload={'metric_id':mid,'baseline':20,'target':30}
    assert admin.post('/api/actions/'+rid+'/configure',json=payload).status_code==200
    term=admin.post('/api/platform/metric-terms',json={'name':'New revenue','definition':'Changed semantic meaning','unit':'yuan'}).json()['id']
    assert admin.post('/api/platform/metric-terms/'+term+'/metrics/'+mid,json={}).status_code==200
    assert admin.post('/api/actions/'+rid+'/measure',json={}).status_code==409
    assert admin.post('/api/actions/'+rid+'/configure',json=payload).status_code==200
    original=analytics.execute_with_evidence
    def drift(sources,metrics,spec):
        result=original(sources,metrics,spec)
        with core.connection() as c:
            c.execute('UPDATE data_sources SET connector_config=? WHERE id=?',('{"retries":2}',sid))
        return result
    monkeypatch.setattr(analytics,'execute_with_evidence',drift)
    assert admin.post('/api/actions/'+rid+'/measure',json={}).status_code==409
    assert core.query('SELECT measurement FROM recommendations WHERE id=?',(rid,),True)['measurement'] is None


def test_scoped_analysis_approval_discloses_local_derived_operations(admin,tmp_path,monkeypatch):
    from app import analysis
    sid,mid,jid,rid,path=analysis_action(admin,tmp_path,monkeypatch)
    other=admin.post('/api/data-sources',json={'name':'Private counterpart','kind':'csv','path':str(path)}).json()['id']
    second=admin.post('/api/data-sources/'+other+'/metrics',json={'name':'Other revenue','table_name':'data',
                      'aggregation':'sum','value_column':'amount'}).json()['id']
    owner,uid=member(admin,'source-owner')
    admin.post('/api/data-sources/'+sid+'/members',json={'user_id':uid,'role':'owner'})
    spec={'items':[{'metric_id':mid,'dimensions':[]},{'metric_id':second,'dimensions':[]}],
          'comparisons':[{'id':'ratio','name':'Coverage ratio','operation':'ratio','left_metric_id':mid,'right_metric_id':second,'dimensions':[]}],
          'scenarios':[{'id':'optimistic','name':'Optimistic assumption','metric_id':mid,'change_percent':1000}]}
    with core.connection() as c:
        c.execute('INSERT INTO analysis_job_sources VALUES(?,?)',(jid,other))
        c.execute("UPDATE analysis_jobs SET status='awaiting_approval',version=1,query_spec=?,brief=? WHERE id=?",
                  (json.dumps(spec),json.dumps({'decision':'Private overall context'}),jid))
    assert owner.get('/api/analysis-jobs/'+jid).status_code==404
    item=owner.get('/api/orchestration/inbox').json()['items'][0]
    assert item['scoped_plan']['scenarios'][0]['change_percent']==1000
    comparison=item['scoped_plan']['comparisons'][0]
    assert comparison['operation']=='ratio' and comparison['right_metric_id'] is None
    assert 'brief' not in item['scoped_plan']
    assert second not in json.dumps(item) and 'Private counterpart' not in json.dumps(item)
    assert owner.post('/api/orchestration/analysis/'+jid+'/approve',json={'version':1,'resource_ids':[sid]}).json()['ready'] is False


def test_source_health_is_authorized_and_observable(admin,tmp_path,monkeypatch):
    sid,mid,jid,rid,path=analysis_action(admin,tmp_path,monkeypatch)
    result=admin.post('/api/data-sources/'+sid+'/health',json={})
    assert result.status_code==200 and result.json()['status']=='ok'
    path.unlink()
    failed=admin.post('/api/data-sources/'+sid+'/health',json={})
    assert failed.status_code==200 and failed.json()['status']=='error'
    assert admin.get('/api/data-sources').json()[0]['connector_health']['status']=='error'
    outsider,_=member(admin,'health-outsider')
    assert outsider.post('/api/data-sources/'+sid+'/health',json={}).status_code==404
