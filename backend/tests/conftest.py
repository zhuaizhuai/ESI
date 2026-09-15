import os
import tempfile
os.environ['ESI_DATA'] = tempfile.mkdtemp(prefix='esi-multiuser-test-')
os.environ['MODEL_API_KEY'] = 'test-only'
os.environ['MODEL_NAME'] = 'test-model'
os.environ['ESI_EXECUTION_MODE'] = 'local'

import pytest
from app.core import connection
from app.auth import password_hash
import time

@pytest.fixture(autouse=True)
def clean_database():
    with connection() as c:
        for table in ('sessions','login_attempts','data_source_members','analysis_events',
                      'analysis_jobs','metrics','data_sources','project_members','events',
                      'audit','tasks','projects','users'):
            c.execute('DELETE FROM '+table)
        c.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?,?)',
                  ('test-admin','admin','Test Admin',password_hash('Admin-test-password-123'),'admin',1,0,time.time()))
    yield


def sign_in(client, username='admin', password='Admin-test-password-123'):
    client.headers['X-ESI-Client']='workbench'
    response=client.post('/api/auth/login',json={'username':username,'password':password})
    assert response.status_code==200,response.text
    client.headers['X-ESI-CSRF']=response.json()['csrf']
    return response.json()
