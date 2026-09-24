"""Cross-source authorization, catalog evidence and recommendation feedback."""
import csv
import json
import time
import httpx
import pytest

from fastapi.testclient import TestClient

from app import analysis_worker, core
from app import analytics, http_connector
from app.main import app
from conftest import sign_in


def member(admin, name='analyst'):
    password='Initial-password-'+name+'-123'
    created=admin.post('/api/users',json={
        'username':name,'display_name':name,'password':password,'role':'member'})
    assert created.status_code==200,created.text
    client=TestClient(app)
    sign_in(client,name,password)
    changed=client.post('/api/auth/password',json={
        'current_password':password,'new_password':'Changed-password-'+name+'-456'})
    assert changed.status_code==200,changed.text
    client.headers['X-ESI-CSRF']=changed.json()['csrf']
    return client,created.json()['id']


def source(admin, tmp_path, name):
    path=tmp_path/(name+'.csv')
    with path.open('w',newline='') as handle:
        writer=csv.writer(handle)
        writer.writerow(['region','amount'])
        writer.writerow(['east','10'])
        writer.writerow(['west','20'])
    response=admin.post('/api/data-sources',json={
        'name':name,'kind':'csv','path':str(path)})
    assert response.status_code==200,response.text
    sid=response.json()['id']
    metric=admin.post(f'/api/data-sources/{sid}/metrics',json={
        'name':name+'金额','table_name':'data','aggregation':'sum',
        'value_column':'amount','dimensions':['region']})
    assert metric.status_code==200,metric.text
    return sid,metric.json()['id']


def test_catalog_knowledge_and_metric_glossary(tmp_path, monkeypatch):
    monkeypatch.setenv('ESI_DATA_ROOTS',str(tmp_path))
    admin=TestClient(app);sign_in(admin)
    viewer,uid=member(admin,'viewer')
    sid,mid=source(admin,tmp_path,'orders')
    first=admin.post('/api/platform/systems',json={'name':'订单系统','description':'订单记录'})
    second=admin.post('/api/platform/systems',json={'name':'库存系统','description':'库存扣减'})
    assert first.status_code==200 and second.status_code==200
    first_id,second_id=first.json()['id'],second.json()['id']
    assert admin.post(f'/api/platform/systems/{first_id}/resources',json={
        'kind':'data_source','resource_id':sid}).status_code==200
    assert admin.post(f'/api/platform/systems/{first_id}/dependencies',json={
        'target_id':second_id,'description':'下单前检查库存'}).status_code==200
    knowledge=admin.post('/api/platform/knowledge',json={
        'system_id':first_id,'title':'订单状态','content':'paid 表示已付款',
        'source_url':'DOC-42'})
    assert knowledge.status_code==200
    assert viewer.get('/api/platform/systems').json()==[]
    assert viewer.get('/api/platform/knowledge').json()==[]
    assert admin.post(f'/api/data-sources/{sid}/members',json={
        'user_id':uid,'role':'viewer'}).status_code==200
    assert len(viewer.get('/api/platform/systems').json())==1
    assert viewer.get('/api/platform/knowledge?q=paid').json()[0]['title']=='订单状态'
    assert viewer.post('/api/platform/knowledge',json={
        'system_id':first_id,'title':'改写','content':'不应允许'}).status_code==403
    term=admin.post('/api/platform/metric-terms',json={
        'name':'销售额','definition':'已付款订单金额求和','unit':'元'})
    assert term.status_code==200
    tid=term.json()['id']
    assert admin.post(f'/api/platform/metric-terms/{tid}/metrics/{mid}',json={}).status_code==200
    terms=viewer.get('/api/platform/metric-terms').json()
    assert terms[0]['metrics'][0]['id']==mid


def test_multi_source_analysis_and_advice_feedback(tmp_path, monkeypatch):
    monkeypatch.setenv('ESI_DATA_ROOTS',str(tmp_path))
    admin=TestClient(app);sign_in(admin)
    analyst,uid=member(admin)
    first,m1=source(admin,tmp_path,'orders')
    second,m2=source(admin,tmp_path,'returns')
    assert admin.post(f'/api/data-sources/{first}/members',json={
        'user_id':uid,'role':'analyst'}).status_code==200
    payload={'source_id':first,'source_ids':[first,second],'title':'跨系统分析',
             'question':'订单与退货对比','report_kind':'strategy'}
    assert analyst.post('/api/analysis-jobs',json=payload).status_code==404
    assert admin.post(f'/api/data-sources/{second}/members',json={
        'user_id':uid,'role':'analyst'}).status_code==200
    created=analyst.post('/api/analysis-jobs',json=payload)
    assert created.status_code==200,created.text
    jid=created.json()['id']
    assert len(analyst.get(f'/api/platform/workflows/analysis/{jid}').json())==5
    assert core.query("SELECT status FROM workflow_steps WHERE job_kind='analysis' AND job_id=? AND step_key='planning'",(jid,),True)['status']=='active'
    monkeypatch.setattr(analysis_worker,'model',lambda messages:{
        'plan':'对比两个系统的金额','query':{'items':[
            {'metric_id':m1,'dimensions':['region']},
            {'metric_id':m2,'dimensions':['region']}],
            'grain':'none','start_date':None,'end_date':None},'_usage':{}})
    core.execute("UPDATE analysis_jobs SET status='planning' WHERE id=?",(jid,))
    analysis_worker.run(core.query('SELECT * FROM analysis_jobs WHERE id=?',(jid,),True))
    detail=analyst.get('/api/analysis-jobs/'+jid).json()
    assert detail['status']=='awaiting_approval'
    assert len(detail['source_ids'])==2
    assert admin.post(f'/api/analysis-jobs/{jid}/approve',json={'version':1}).status_code==200
    assert core.query("SELECT status FROM workflow_steps WHERE job_kind='analysis' AND job_id=? AND step_key='approval'",(jid,),True)['status']=='done'
    monkeypatch.setattr(analysis_worker,'model',lambda messages:{
        'done':True,'summary':'两个系统的金额均为 30。',
        'recommendations':[{'title':'核对退货','action':'检查异常退货',
                            'success_metric':'退货金额'}], '_usage':{}})
    analysis_worker.run(core.query('SELECT * FROM analysis_jobs WHERE id=?',(jid,),True))
    detail=analyst.get('/api/analysis-jobs/'+jid).json()
    assert detail['status']=='review'
    assert core.query("SELECT status FROM workflow_steps WHERE job_kind='analysis' AND job_id=? AND step_key='acceptance'",(jid,),True)['status']=='active'
    assert {row['source_name'] for row in detail['result']['rows']}=={'orders','returns'}
    recs=analyst.get(f'/api/platform/analysis-jobs/{jid}/recommendations').json()
    assert len(recs)==1
    rid=recs[0]['id']
    assert analyst.post(f'/api/platform/recommendations/{rid}/feedback',json={
        'status':'done','outcome':'已检查'}).status_code==403
    assert admin.post(f'/api/platform/recommendations/{rid}/assign',json={
        'assignee_id':uid}).status_code==200
    assert analyst.post(f'/api/platform/recommendations/{rid}/feedback',json={
        'status':'done','outcome':'已核对两套数据'}).status_code==200
    assert admin.get(f'/api/platform/analysis-jobs/{jid}/recommendations').json()[0]['status']=='done'
    assert admin.post(f'/api/data-sources/{second}/members',json={
        'user_id':uid,'role':'remove'}).status_code==200
    assert analyst.get('/api/analysis-jobs/'+jid).status_code==404
    assert analyst.get(f'/api/platform/analysis-jobs/{jid}/recommendations').status_code==404


def test_readonly_https_json_connector_is_allowlisted_and_bounded(monkeypatch):
    admin=TestClient(app);sign_in(admin)
    monkeypatch.setenv('ESI_CONNECTOR_HOSTS','api.example.internal')
    monkeypatch.setenv('ESI_CONNECTOR_TOKEN_ORDERS','test-token')
    def handler(request):
        assert request.method=='GET'
        assert request.headers['authorization']=='Bearer test-token'
        return httpx.Response(200,headers={'content-type':'application/json'},json=[
            {'region':'east','amount':10},{'region':'west','amount':20}])
    transport=httpx.MockTransport(handler)
    real_client=httpx.Client
    monkeypatch.setattr(http_connector.httpx,'Client',
                        lambda **kwargs: real_client(transport=transport,**kwargs))
    url='https://api.example.internal/orders'
    denied=admin.post('/api/data-sources',json={
        'name':'bad','kind':'http_json','path':'https://other.example/orders'})
    assert denied.status_code==400
    created=admin.post('/api/data-sources',json={
        'name':'API 订单','kind':'http_json','path':url,
        'auth_env':'ESI_CONNECTOR_TOKEN_ORDERS'})
    assert created.status_code==200,created.text
    sid=created.json()['id']
    response=admin.post(f'/api/data-sources/{sid}/metrics',json={
        'name':'订单金额','table_name':'data','aggregation':'sum',
        'value_column':'amount','dimensions':['region']})
    assert response.status_code==200,response.text
    source=core.query('SELECT * FROM data_sources WHERE id=?',(sid,),True)
    metric=core.query('SELECT * FROM metrics WHERE id=?',(response.json()['id'],),True)
    spec={'items':[{'metric_id':metric['id'],'dimensions':['region']}],
          'grain':'none','start_date':None,'end_date':None}
    _,rows=analytics.execute(source,[metric],spec)
    assert {row['value'] for row in rows}=={10.0,20.0}
    assert 'test-token' not in str(admin.get('/api/data-sources').json())


def test_source_symlink_is_rejected(tmp_path,monkeypatch):
    monkeypatch.setenv('ESI_DATA_ROOTS',str(tmp_path))
    target=tmp_path/'sales.csv';target.write_text('amount\n1\n')
    alias=tmp_path/'alias.csv';alias.symlink_to(target)
    with pytest.raises(ValueError,match='符号链接'):
        analytics.source_path(alias)
