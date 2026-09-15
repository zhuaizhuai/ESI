import json
import time
import subprocess
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException
from app.main import app
from app import core, worker
from app.auth import password_hash, get_session
from conftest import sign_in

@pytest.fixture
def team(tmp_path):
    admin=TestClient(app);sign_in(admin)
    clients={}
    for name in ('lead','dev','viewer','outsider'):
        password=name+'-initial-password-123'
        r=admin.post('/api/users',json={'username':name,'display_name':name,'password':password,'role':'member'})
        assert r.status_code==200,r.text
        c=TestClient(app);login=sign_in(c,name,password)
        assert login['user']['must_change_password']==1
        assert c.get('/api/projects').status_code==403
        r=c.post('/api/auth/password',json={'current_password':password,'new_password':name+'-changed-password-456'})
        assert r.status_code==200,r.text
        c.headers['X-ESI-CSRF']=r.json()['csrf']
        clients[name]=(c,login['user']['id'])
    repo=tmp_path/'repo';repo.mkdir();(repo/'README.md').write_text('test')
    subprocess.run(['git','init',str(repo)],check=True,capture_output=True)
    subprocess.run(['git','-C',str(repo),'add','.'],check=True)
    subprocess.run(['git','-C',str(repo),'-c','user.name=Test','-c','user.email=t@localhost','commit','-m','init'],check=True,capture_output=True)
    r=admin.post('/api/projects',json={'name':'Private','path':str(repo),'checks':['python3 -c "pass"']})
    assert r.status_code==200,r.text
    pid=r.json()['id']
    for name,role in [('lead','maintainer'),('dev','developer'),('viewer','viewer')]:
        assert admin.post(f'/api/projects/{pid}/members',json={'user_id':clients[name][1],'role':role}).status_code==200
    dev=clients['dev'][0]
    r=dev.post('/api/tasks',json={'project_id':pid,'title':'feature','requirement':'feature','acceptance':'pass'})
    assert r.status_code==200,r.text
    return admin,clients,pid,r.json()['id']

def test_anonymous_requests_are_rejected():
    c=TestClient(app)
    for path in ('/api/projects','/api/tasks','/api/settings','/api/users','/api/audit','/api/tasks/missing/stream'):
        assert c.get(path).status_code==401
    assert c.post('/api/auth/login',json={'username':'admin','password':'Admin-test-password-123'}).status_code==403

def test_no_project_idor_and_readonly(team):
    admin,users,pid,tid=team
    outsider=users['outsider'][0];viewer=users['viewer'][0]
    assert outsider.get('/api/projects').json()==[]
    assert outsider.get('/api/tasks').json()==[]
    for suffix in ('','/stream'):
        assert outsider.get('/api/tasks/'+tid+suffix).status_code==404
    assert outsider.post('/api/tasks/'+tid+'/cancel',json={}).status_code==404
    assert viewer.get('/api/tasks/'+tid).status_code==200
    assert viewer.post('/api/tasks/'+tid+'/cancel',json={}).status_code==403
    assert viewer.post('/api/tasks',json={'project_id':pid,'title':'x','requirement':'x','acceptance':'x'}).status_code==403
    assert viewer.get('/api/users').status_code==403
    assert users['dev'][0].post('/api/projects',json={'name':'x','path':'/tmp','checks':['echo x']}).status_code==403

def test_only_maintainer_can_approve_and_revoke_stops_worker(team):
    admin,users,pid,tid=team
    core.update(tid,status='awaiting_approval',plan='方案',version=1)
    dev=users['dev'][0];lead=users['lead'][0]
    assert dev.post('/api/tasks/'+tid+'/approve',json={'version':1}).status_code==403
    assert lead.post('/api/tasks/'+tid+'/approve',json={'version':1}).status_code==200
    worker.active(tid)
    t=lead.get('/api/tasks/'+tid).json()
    assert t['created_by']==users['dev'][1]
    assert t['approved_by']==users['lead'][1]
    assert any(e['actor_name']=='lead' for e in t['events'])
    assert admin.post('/api/projects/'+pid+'/members',json={'user_id':users['lead'][1],'role':'viewer'}).status_code==200
    with pytest.raises(InterruptedError):worker.active(tid)
    assert core.query('SELECT status FROM tasks WHERE id=?',(tid,),True)['status']=='failed'

def test_csrf_logout_and_expiry():
    c=TestClient(app);sign_in(c)
    token=c.cookies.get('esi_session')
    c.headers.pop('X-ESI-CSRF')
    assert c.post('/api/auth/logout',json={}).status_code==403
    me=c.get('/api/auth/me').json();c.headers['X-ESI-CSRF']=me['csrf']
    assert c.post('/api/auth/logout',json={}).status_code==200
    c.cookies.set('esi_session',token)
    assert c.get('/api/projects').status_code==401
    c.cookies.clear();sign_in(c)
    core.execute('UPDATE sessions SET expires=?',(time.time()-1,))
    assert c.get('/api/projects').status_code==401

def test_disable_invalidates_sessions_and_password_reset(team):
    admin,users,pid,tid=team
    dev,uid=users['dev']
    assert admin.post('/api/users/'+uid,json={'role':'member','active':False}).status_code==200
    assert dev.get('/api/tasks/'+tid).status_code==401
    assert admin.post('/api/users/test-admin',json={'role':'member','active':False}).status_code==409
    assert admin.post('/api/users/'+users['lead'][1]+'/password',json={'password':'New-temporary-password-789'}).status_code==200
    assert users['lead'][0].get('/api/projects').status_code==401
    r=sign_in(users['lead'][0],'lead','New-temporary-password-789')
    assert r['user']['must_change_password']==1

def test_login_rate_limit_and_no_plaintext():
    c=TestClient(app,headers={'X-ESI-Client':'workbench'})
    for _ in range(10):
        assert c.post('/api/auth/login',json={'username':'admin','password':'incorrect'}).status_code==401
    assert c.post('/api/auth/login',json={'username':'admin','password':'incorrect'}).status_code==429
    row=core.query('SELECT * FROM users WHERE id=?',('test-admin',),True)
    assert row['password_hash'].startswith('pbkdf2_sha256$600000$')
    assert 'Admin-test-password' not in row['password_hash']

def test_cross_origin_write_is_rejected():
    c=TestClient(app,headers={'X-ESI-Client':'workbench','Origin':'https://evil.example'})
    assert c.post('/api/auth/login',json={'username':'admin','password':'Admin-test-password-123'}).status_code==403

def test_container_policy_is_fail_closed(tmp_path,monkeypatch):
    from app import execution
    monkeypatch.setenv('ESI_EXECUTION_MODE','docker')
    monkeypatch.setattr(execution.shutil,'which',lambda _:None)
    with pytest.raises(RuntimeError,match='Docker'):
        execution.preflight()
    args,name=execution.command_spec(tmp_path,'python3 -m unittest')
    assert args[0:2]==['docker','run']
    assert '--network' in args and args[args.index('--network')+1]=='none'
    assert '--read-only' in args
    assert '--cap-drop' in args
    assert '--privileged' not in args
    assert all('docker.sock' not in x for x in args)
    assert any('/workspace/.git,readonly' in x for x in args)
    assert name.startswith('esi-check-')

def test_legacy_database_migration_and_bootstrap(tmp_path):
    import sqlite3, os, sys
    root=tmp_path/'legacy';root.mkdir()
    c=sqlite3.connect(root/'esi.db')
    c.executescript('''
      CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT,path TEXT,branch TEXT,checks TEXT);
      CREATE TABLE tasks(id TEXT PRIMARY KEY,project_id TEXT,title TEXT,requirement TEXT,acceptance TEXT,status TEXT,plan TEXT,version INTEGER DEFAULT 0,approved INTEGER,worktree TEXT,base TEXT,result TEXT,created REAL);
      CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT,kind TEXT,message TEXT,created REAL);
      INSERT INTO projects VALUES('old-project','Original','/tmp/repo','HEAD','[]');
      INSERT INTO tasks(id,project_id,title,status,plan,version,approved) VALUES('old-task','old-project','Keep me','queued','approved before users',1,1);
    ''');c.commit();c.close()
    env={**os.environ,'ESI_DATA':str(root)}
    result=subprocess.run([sys.executable,'-m','app.manage'],env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    c=sqlite3.connect(root/'esi.db');c.row_factory=sqlite3.Row
    task=dict(c.execute('SELECT * FROM tasks').fetchone())
    assert task['title']=='Keep me'
    assert task['created_by']
    assert task['status']=='awaiting_approval' and task['approved'] is None
    assert c.execute('SELECT COUNT(*) FROM users').fetchone()[0]==1
    credential=root/'bootstrap-admin.txt'
    assert credential.stat().st_mode & 0o777 == 0o600
    assert '临时密码' not in result.stdout
    result=subprocess.run([sys.executable,'-m','app.manage'],env=env,capture_output=True,text=True)
    assert result.returncode==0
    assert c.execute('SELECT COUNT(*) FROM users').fetchone()[0]==1
    c.close()
