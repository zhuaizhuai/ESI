import json, subprocess
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import core, worker

client=TestClient(app)

@pytest.fixture(autouse=True)
def authenticated_admin(clean_database):
    from conftest import sign_in
    sign_in(client)

@pytest.fixture
def project(tmp_path):
    repo=tmp_path/'repo'; repo.mkdir()
    (repo/'calculator.py').write_text('def add(a, b):\n    return a + b\n')
    subprocess.run(['git','init',str(repo)],check=True,capture_output=True)
    subprocess.run(['git','-C',str(repo),'add','.'],check=True)
    subprocess.run(['git','-C',str(repo),'-c','user.name=Test','-c','user.email=test@localhost','commit','-m','init'],check=True,capture_output=True)
    r=client.post('/api/projects',json={'name':'test','path':str(repo),'checks':['python3 -c "from calculator import add; assert add(1,2)==3"']})
    assert r.status_code==200
    return r.json()['id'],repo

def create(pid):
    r=client.post('/api/tasks',json={'project_id':pid,'title':'增加减法','requirement':'增加 subtract','acceptance':'subtract(5, 2) == 3'})
    assert r.status_code==200
    return r.json()['id']

def test_path_escape_and_symlink(tmp_path):
    root=tmp_path/'root'; root.mkdir(); outside=tmp_path/'outside';outside.mkdir()
    (root/'link').symlink_to(outside,target_is_directory=True)
    for name in ['../outside/file','link/file','.git/config','.env','.env.local']:
        with pytest.raises(ValueError): core.safe_path(root,name)
    assert core.safe_path(root,'src/main.py')==root/'src/main.py'

def test_approval_and_version(project):
    tid=create(project[0])
    assert client.post(f'/api/tasks/{tid}/approve',json={'version':0}).status_code==409
    core.update(tid,status='awaiting_approval',plan='方案',version=1)
    assert client.post(f'/api/tasks/{tid}/plan',json={'plan':'更新方案','version':1}).status_code==200
    assert client.post(f'/api/tasks/{tid}/approve',json={'version':1}).status_code==409
    assert client.post(f'/api/tasks/{tid}/approve',json={'version':2}).status_code==200
    assert client.post(f'/api/tasks/{tid}/approve',json={'version':2}).status_code==409

def test_cancel_cannot_be_overwritten(project):
    tid=create(project[0]); client.post(f'/api/tasks/{tid}/cancel')
    core.update(tid,status='failed')
    assert core.query('SELECT status FROM tasks WHERE id=?',(tid,),True)['status']=='cancelled'
    with pytest.raises(InterruptedError): worker.active(tid)

def test_real_git_and_validation_with_scripted_model(project,monkeypatch):
    pid,repo=project;tid=create(pid)
    responses=iter([{'tool':'read_file','args':{'path':'calculator.py'}},{'done':True,'summary':'增加 subtract 并验证加法不变。'}])
    monkeypatch.setattr(worker,'model',lambda messages:next(responses))
    core.update(tid,status='analyzing')
    worker.run(core.query('SELECT * FROM tasks WHERE id=?',(tid,),True))
    t=client.get(f'/api/tasks/{tid}').json()
    assert t['status']=='awaiting_approval'
    assert 'subtract' not in (Path(t['worktree'])/'calculator.py').read_text()
    assert client.post(f'/api/tasks/{tid}/approve',json={'version':t['version']}).status_code==200
    responses=iter([{'tool':'write_file','args':{'path':'calculator.py','content':'def add(a,b):\n    return a+b\n\ndef subtract(a,b):\n    return a-b\n'}},{'done':True,'summary':'实现完成'}])
    worker.run(core.query('SELECT * FROM tasks WHERE id=?',(tid,),True))
    t=client.get(f'/api/tasks/{tid}').json()
    assert t['status']=='review'
    assert '+def subtract' in t['result']['diff']
    assert t['result']['checks'][0]['exit_code']==0
    assert 'subtract' not in (repo/'calculator.py').read_text()
    assert client.post(f'/api/tasks/{tid}/complete').status_code==200


def test_two_repositories_are_researched_and_changed_after_approval(project,tmp_path,monkeypatch):
    first,repo=project
    second_repo=tmp_path/'service-b';second_repo.mkdir()
    (second_repo/'service.py').write_text('def value():\n    return 1\n')
    subprocess.run(['git','init',str(second_repo)],check=True,capture_output=True)
    subprocess.run(['git','-C',str(second_repo),'add','.'],check=True)
    subprocess.run(['git','-C',str(second_repo),'-c','user.name=Test',
                    '-c','user.email=test@localhost','commit','-m','init'],check=True,capture_output=True)
    response=client.post('/api/projects',json={'name':'service-b','path':str(second_repo),
       'checks':['python3 -c "from service import value; assert value()==2"']})
    assert response.status_code==200,response.text
    second=response.json()['id']
    created=client.post('/api/tasks',json={'project_id':first,'project_ids':[first,second],
       'title':'跨系统变更','requirement':'更新两个服务','acceptance':'两个仓库验证通过'})
    assert created.status_code==200,created.text
    tid=created.json()['id']
    replies=iter([
        {'tool':'read_file','args':{'project_id':first,'path':'calculator.py'}},
        {'tool':'read_file','args':{'project_id':second,'path':'service.py'}},
        {'done':True,'summary':'两个仓库都需要修改，并分别执行验证。'},
    ])
    monkeypatch.setattr(worker,'model',lambda messages:next(replies))
    core.update(tid,status='analyzing')
    worker.run(core.query('SELECT * FROM tasks WHERE id=?',(tid,),True))
    detail=client.get(f'/api/tasks/{tid}').json()
    assert detail['status']=='awaiting_approval'
    assert len(detail['project_runs'])==2
    assert client.post(f'/api/tasks/{tid}/approve',json={'version':detail['version']}).status_code==200
    replies=iter([
        {'tool':'write_file','args':{'project_id':first,'path':'calculator.py',
           'content':'def add(a,b):\n    return a+b\n\ndef subtract(a,b):\n    return a-b\n'}},
        {'tool':'write_file','args':{'project_id':second,'path':'service.py',
           'content':'def value():\n    return 2\n'}},
        {'done':True,'summary':'两个仓库修改完成'},
    ])
    monkeypatch.setattr(worker,'model',lambda messages:next(replies))
    worker.run(core.query('SELECT * FROM tasks WHERE id=?',(tid,),True))
    detail=client.get(f'/api/tasks/{tid}').json()
    assert detail['status']=='review'
    assert len(detail['result']['checks'])==2
    assert {check['project_id'] for check in detail['result']['checks']}=={first,second}
    assert 'service-b' in detail['result']['diff']
    assert 'return 1' in (second_repo/'service.py').read_text()

def test_failed_checks_cannot_complete(project,monkeypatch):
    pid,repo=project;tid=create(pid)
    core.execute('UPDATE projects SET checks=? WHERE id=?',(json.dumps(['python3 -c "raise SystemExit(1)"']),pid))
    core.update(tid,status='queued',plan='test',version=1,approved=1,approved_by='test-admin')
    monkeypatch.setattr(worker,'model',lambda messages:{'done':True,'summary':'完成'})
    worker.run(core.query('SELECT * FROM tasks WHERE id=?',(tid,),True))
    assert client.get(f'/api/tasks/{tid}').json()['status']=='failed'
    assert client.post(f'/api/tasks/{tid}/complete').status_code==409

def test_planning_tools_cannot_write(project,monkeypatch):
    pid,repo=project;tid=create(pid)
    core.update(tid,status='analyzing')
    replies=iter([{'tool':'write_file','args':{'path':'calculator.py','content':'bad'}},{'done':True,'summary':'方案'}])
    monkeypatch.setattr(worker,'model',lambda messages:next(replies))
    t=core.query('SELECT * FROM tasks WHERE id=?',(tid,),True)
    p=core.query('SELECT * FROM projects WHERE id=?',(pid,),True)
    worker.loop(t,repo,p,True)
    assert 'return a + b' in (repo/'calculator.py').read_text()

def test_cancel_stops_validation_process(project):
    import threading, time
    pid,repo=project;tid=create(pid)
    core.update(tid,status='running',version=1,approved=1,approved_by='test-admin')
    timer=threading.Timer(.15,lambda:client.post(f'/api/tasks/{tid}/cancel'))
    timer.start(); start=time.time()
    try:
        with pytest.raises(InterruptedError):
            worker.checks(tid,repo,['python3 -c "import time; time.sleep(30)"'])
    finally: timer.join()
    assert time.time()-start<5
