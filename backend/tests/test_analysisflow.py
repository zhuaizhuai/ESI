import csv
import json
import sqlite3
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException

from app import analytics, analysis_worker, core, http_connector
from app.analysis import init_analysis, freeze_approval, check_approval_snapshot
from app.main import app
from app.platform import sync_workflow
from conftest import sign_in


def metric(mid='m',sid='s',dimension='region'):
    return {'id':mid,'source_id':sid,'name':mid,'description':'test','table_name':'data',
            'aggregation':'sum','value_column':'amount','date_column':'day',
            'dimensions':[dimension],'term_unit':'CNY'}


def csv_source(tmp_path,monkeypatch,sid,rows,dimension='region'):
    monkeypatch.setenv('ESI_DATA_ROOTS',str(tmp_path))
    path=tmp_path/(sid+'.csv')
    with path.open('w',newline='') as handle:
        writer=csv.writer(handle);writer.writerow([dimension,'amount','day']);writer.writerows(rows)
    return {'id':sid,'name':sid,'kind':'csv','path':str(path)}


def test_group_limit_fails_instead_of_returning_partial_csv_or_sqlite(tmp_path,monkeypatch):
    source=csv_source(tmp_path,monkeypatch,'s',[(str(n),1,'2026-01-01') for n in range(201)])
    m=metric();spec={'items':[{'metric_id':'m','dimensions':['region']} ]}
    with pytest.raises(ValueError,match='200'): analytics.execute(source,[m],spec)
    path=tmp_path/'many.db'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE data(region TEXT,amount REAL,day TEXT)')
        c.executemany('INSERT INTO data VALUES(?,?,?)',[(str(n),1,'2026-01-01') for n in range(201)])
    with pytest.raises(ValueError,match='200'):
        analytics.execute({**source,'kind':'sqlite','path':str(path)},[m],spec)


def test_http_zero_false_null_are_distinct_groups():
    m=metric();m['aggregation']='count'
    m['date_column']=None;m['value_column']=None
    result=analytics._http_metric([{'region':0},{'region':False},{'region':None},{'region':''}],m,['region'],'none',None,None)
    assert len(result)==4
    assert {(type(r['region']).__name__,r['region'],r['value']) for r in result}=={
        ('int',0,1),('bool',False,1),('NoneType',None,1),('str','',1)}


def test_date_boundary_and_quality_consistent(tmp_path,monkeypatch):
    records=[('east','2','2026-01-31T23:59:59.999'),('east','3','2026-01-31 23:59:59'),
             ('east','4','2026-02-01'),('east','bad','2026-01-31'),('east','10','invalid')]
    source=csv_source(tmp_path,monkeypatch,'s',records)
    spec={'items':[{'metric_id':'m','dimensions':['region']}], 'grain':'day',
          'start_date':'2026-01-31','end_date':'2026-01-31'}
    _,rows,evidence=analytics.execute_with_evidence([source],[metric()],spec)
    assert rows[0]['value']==5
    q=evidence['quality']['m'];assert q['invalid_numeric_rows']==q['invalid_date_rows']==1
    assert q['scanned_rows']==5 and q['included_rows']==2 and q['complete'] is True
    db=tmp_path/'dates.db'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE data(region TEXT,amount TEXT,day TEXT)');c.executemany('INSERT INTO data VALUES(?,?,?)',records)
    _,sql_rows,sql_evidence=analytics.execute_with_evidence([{**source,'kind':'sqlite','path':str(db)}],[metric()],spec)
    assert sql_rows[0]['value']==5 and sql_evidence['quality']['m']['invalid_date_rows']==1
    assert len(rows[0]['evidence_id'])>20 and evidence['sources'][0]['collected_at']>=evidence['sources'][0]['started_at']


@pytest.mark.parametrize('spec',[None,[],{'items':[None]}, {'items':[{'metric_id':'m','dimensions':['region','region']}]},
    {'items':[{'metric_id':'m','dimensions':['value']}]},
    {'items':[{'metric_id':'m'}],'start_date':123}])
def test_invalid_query_schema_is_rejected(spec):
    with pytest.raises(ValueError): analytics.validate_spec(spec,[metric()])


def test_cross_source_alignment_missing_groups_zero_and_scenario(tmp_path,monkeypatch):
    left=csv_source(tmp_path,monkeypatch,'l',[('east',20,'2026-01-01'),('west',5,'2026-01-01'),('north',7,'2026-01-01')])
    right=csv_source(tmp_path,monkeypatch,'r',[('east',10,'2026-01-01'),('west',0,'2026-01-01')],'area')
    metrics=[metric('a','l'),metric('b','r','area')]
    spec={'items':[{'metric_id':'a','dimensions':['region'],'dimension_aliases':{'region':'area'}},
                   {'metric_id':'b','dimensions':['area']}],
          'comparisons':[{'id':'growth','name':'Growth','operation':'percentage_change',
                          'left_metric_id':'a','right_metric_id':'b','dimensions':['area']}],
          'scenarios':[{'id':'scenario','name':'up10','metric_id':'a','change_percent':10}]}
    normalized,rows,evidence=analytics.execute_with_evidence([left,right],metrics,spec)
    comparisons={r['dimensions']['area']:r for r in evidence['calculations'] if r['kind']=='comparison'}
    assert comparisons['east']['value']==100 and comparisons['east']['status']=='computed'
    assert comparisons['west']['status']=='zero_denominator' and comparisons['west']['value'] is None
    assert comparisons['north']['status']=='missing_group' and comparisons['north']['value'] is None
    scenarios=[r for r in evidence['calculations'] if r['kind']=='scenario']
    assert next(r for r in scenarios if r['dimensions']['area']=='east')['value']==22
    assert all(r['status']=='assumption' for r in scenarios)
    assert all(r['evidence_ids'] for r in evidence['calculations'])
    metrics[1]['term_unit']='USD'
    with pytest.raises(ValueError,match='单位'): analytics.validate_spec(spec,metrics)
    metrics[1]['term_unit']='CNY';spec['comparisons'][0]['dimensions']=[]
    with pytest.raises(ValueError,match='公共维度'): analytics.validate_spec(spec,metrics)


def test_http_evidence_mock_does_not_expose_raw_detail(monkeypatch):
    monkeypatch.setenv('ESI_CONNECTOR_HOSTS','test.internal')
    transport=httpx.MockTransport(lambda request:httpx.Response(200,headers={'content-type':'application/json'},
        json=[{'region':'east','amount':2,'day':'2026-01-01','private_detail':'DO-NOT-EXPOSE'}]))
    real=httpx.Client
    monkeypatch.setattr(http_connector.httpx,'Client',lambda **kw:real(transport=transport,**kw))
    source={'id':'s','name':'api','kind':'http_json','path':'https://test.internal/data'}
    _,rows,evidence=analytics.execute_with_evidence([source],[metric()],{'items':[{'metric_id':'m','dimensions':['region']}]})
    assert rows[0]['value']==2 and 'DO-NOT-EXPOSE' not in json.dumps(evidence)


def create_job(tmp_path,monkeypatch):
    init_analysis()
    client=TestClient(app);sign_in(client)
    source=csv_source(tmp_path,monkeypatch,'data',[('east',2,'2026-01-01')])
    sid=client.post('/api/data-sources',json={'name':'data','kind':'csv','path':source['path']}).json()['id']
    mid=client.post(f'/api/data-sources/{sid}/metrics',json={'name':'sales','table_name':'data','aggregation':'sum',
        'value_column':'amount','date_column':'day','dimensions':['region']}).json()['id']
    jid=client.post('/api/analysis-jobs',json={'source_id':sid,'title':'report','question':'sales'}).json()['id']
    spec={'items':[{'metric_id':mid,'dimensions':['region']} ]}
    core.execute("UPDATE analysis_jobs SET status='awaiting_approval',plan='approved',version=1,query_spec=? WHERE id=?",(json.dumps(spec),jid))
    return client,jid,sid,mid


def test_approval_freezes_configuration_and_rejects_term_rebinding(tmp_path,monkeypatch):
    _,jid,sid,mid=create_job(tmp_path,monkeypatch)
    with core.connection() as c:
        job=dict(c.execute('SELECT * FROM analysis_jobs WHERE id=?',(jid,)).fetchone());frozen=freeze_approval(c,job)
    assert frozen['metrics'][0]['id']==mid
    with core.connection() as c:
        job=dict(c.execute('SELECT * FROM analysis_jobs WHERE id=?',(jid,)).fetchone())
        assert check_approval_snapshot(c,job)[3]['configuration_hash']==frozen['configuration_hash']
        c.execute('INSERT INTO metric_terms VALUES(?,?,?,?,?,?)',('term','Money','Definition','CNY','test-admin',time.time()))
        c.execute('INSERT INTO metric_term_links VALUES(?,?)',(mid,'term'))
        with pytest.raises(ValueError,match='配置发生变化'): check_approval_snapshot(c,job)


def test_live_data_change_allowed_but_connector_config_change_requires_approval(tmp_path,monkeypatch):
    _,jid,sid,mid=create_job(tmp_path,monkeypatch)
    with core.connection() as c:
        job=dict(c.execute('SELECT * FROM analysis_jobs WHERE id=?',(jid,)).fetchone())
        freeze_approval(c,job)
    source=core.query('SELECT * FROM data_sources WHERE id=?',(sid,),True)
    with open(source['path'],'a') as handle: handle.write('east,5,2026-01-01\n')
    with core.connection() as c:
        job=dict(c.execute('SELECT * FROM analysis_jobs WHERE id=?',(jid,)).fetchone())
        sources,metrics,spec,_=check_approval_snapshot(c,job)
        assert analytics.execute_with_evidence(sources,metrics,spec)[1][0]['value']==7
        c.execute('UPDATE data_sources SET connector_config=? WHERE id=?',('{"retries":1}',sid))
        with pytest.raises(ValueError,match='配置发生变化'): check_approval_snapshot(c,job)


def test_brief_versions_and_evidence_permissions(tmp_path,monkeypatch):
    client,jid,sid,mid=create_job(tmp_path,monkeypatch)
    before=client.get(f'/api/analysis/jobs/{jid}/brief');assert before.status_code==200,before.text
    response=client.post(f'/api/analysis/jobs/{jid}/brief',json={'version':1,'brief':{'decision':'budget','objective':'growth'}})
    assert response.status_code==200 and response.json()['version']==2
    assert client.post(f'/api/analysis/jobs/{jid}/brief',json={'version':1,'brief':{}}).status_code==409
    assert client.get(f'/api/analysis/jobs/{jid}/evidence').status_code==409
    assert client.post(f'/api/analysis-jobs/{jid}/approve',json={'version':2}).status_code==200
    def model(messages):
        payload=json.loads(messages[1]['content'])
        assert 'private_detail' not in payload
        return {'done':True,'summary':'Observed sales 2','claims':[{'text':'Sales are 2','kind':'observation',
            'evidence_ids':[payload['rows'][0]['evidence_id']]}]}
    monkeypatch.setattr(analysis_worker,'model',model)
    analysis_worker.run(core.query('SELECT * FROM analysis_jobs WHERE id=?',(jid,),True))
    response=client.get(f'/api/analysis/jobs/{jid}/evidence');assert response.status_code==200,response.text
    assert response.json()['claim_validation']=='references_validated_content_requires_review'
    assert response.json()['approval_snapshot']['brief']['decision']=='budget'
    assert TestClient(app).get(f'/api/analysis/jobs/{jid}/evidence').status_code==401
    password='New-password-123456'
    uid=client.post('/api/users',json={'username':'outsider','display_name':'Outsider','password':password,'role':'member'}).json()['id']
    outsider=TestClient(app);sign_in(outsider,'outsider',password)
    changed=outsider.post('/api/auth/password',json={'current_password':password,'new_password':'Changed-password-654321'})
    outsider.headers['X-ESI-CSRF']=changed.json()['csrf']
    assert outsider.get(f'/api/analysis/jobs/{jid}/evidence').status_code==404
    client.post(f'/api/data-sources/{sid}/members',json={'user_id':uid,'role':'viewer'})
    assert outsider.get(f'/api/analysis/jobs/{jid}/evidence').status_code==200
    client.post(f'/api/data-sources/{sid}/members',json={'user_id':uid,'role':'remove'})
    assert outsider.get(f'/api/analysis/jobs/{jid}/evidence').status_code==404


def test_failed_model_keeps_deterministic_evidence(tmp_path,monkeypatch):
    client,jid,_,_=create_job(tmp_path,monkeypatch)
    assert client.post(f'/api/analysis-jobs/{jid}/approve',json={'version':1}).status_code==200
    monkeypatch.setattr(analysis_worker,'model',lambda messages:{'done':True,'summary':'bad',
        'claims':[{'text':'fabricated','kind':'observation','evidence_ids':['unknown']}]})
    analysis_worker.run(core.query('SELECT * FROM analysis_jobs WHERE id=?',(jid,),True))
    assert core.query('SELECT status FROM analysis_jobs WHERE id=?',(jid,),True)['status']=='failed'
    response=client.get(f'/api/analysis/jobs/{jid}/evidence')
    assert response.status_code==200 and response.json()['rows'][0]['value']==2
    assert response.json()['claim_validation']=='model_pending'


def test_plan_api_uses_unified_units_for_comparisons(tmp_path,monkeypatch):
    client,jid,sid,first=create_job(tmp_path,monkeypatch)
    second=client.post(f'/api/data-sources/{sid}/metrics',json={'name':'other','table_name':'data','aggregation':'sum',
        'value_column':'amount','date_column':'day','dimensions':['region']}).json()['id']
    term=client.post('/api/platform/metric-terms',json={'name':'Money','definition':'Sum of amounts','unit':'CNY'}).json()['id']
    for mid in (first,second):
        assert client.post(f'/api/platform/metric-terms/{term}/metrics/{mid}',json={}).status_code==200
    spec={'items':[{'metric_id':mid,'dimensions':['region']} for mid in (first,second)],
          'comparisons':[{'id':'diff','name':'Difference','operation':'difference',
                          'left_metric_id':first,'right_metric_id':second,'dimensions':['region']}]}
    response=client.post(f'/api/analysis-jobs/{jid}/plan',json={'version':1,'plan':'Compare approved measures','query_spec':spec})
    assert response.status_code==200,response.text
    assert client.post(f'/api/analysis-jobs/{jid}/approve',json={'version':2}).status_code==200


@pytest.mark.parametrize('terminal',['cancelled','interrupted'])
def test_concurrent_terminal_state_is_not_overwritten_by_authorization_failure(tmp_path,monkeypatch,terminal):
    _,jid,_,_=create_job(tmp_path,monkeypatch)
    with core.connection() as c:
        c.execute("UPDATE analysis_jobs SET status='analyzing' WHERE id=?",(jid,))
        sync_workflow(c,'analysis',jid,'analyzing')
    expected=[]
    def permission_check(c,user,job,minimum='viewer'):
        # Separate connection commits the concurrent terminal transition between read and failure.
        with core.connection() as concurrent:
            concurrent.execute('UPDATE analysis_jobs SET status=? WHERE id=?',(terminal,jid))
            sync_workflow(concurrent,'analysis',jid,terminal)
            expected.extend(dict(r) for r in concurrent.execute(
                "SELECT step_key,status FROM workflow_steps WHERE job_kind='analysis' AND job_id=? ORDER BY position",(jid,)))
        raise HTTPException(403,'permission revoked during cancellation')
    monkeypatch.setattr(analysis_worker,'require_analysis_sources',permission_check)
    with pytest.raises(InterruptedError,match='permission revoked'):
        analysis_worker.active(jid)
    assert core.query('SELECT status FROM analysis_jobs WHERE id=?',(jid,),True)['status']==terminal
    actual=core.query("SELECT step_key,status FROM workflow_steps WHERE job_kind='analysis' AND job_id=? ORDER BY position",(jid,))
    assert actual==expected
    assert next(row for row in actual if row['step_key']=='aggregation')['status']==terminal


def test_unknown_report_reference_is_rejected(monkeypatch):
    monkeypatch.setattr(analysis_worker,'model',lambda messages:{'done':True,'summary':'report',
        'claims':[{'text':'bad','kind':'observation','evidence_ids':['invented']}]})
    with pytest.raises(ValueError,match='未知'):
        analysis_worker.report({'question':'q','plan':'p','report_kind':'report'},[],[],{'items':[]},[],{})
