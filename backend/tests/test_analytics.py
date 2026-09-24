import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import analysis_worker, core
from app.analytics import _csv_metric, _sqlite_metric
from conftest import sign_in


def make_sales(path):
    connection=sqlite3.connect(path)
    connection.execute('''CREATE TABLE orders(
      order_date TEXT, region TEXT, channel TEXT, customer_id TEXT,
      amount REAL, status TEXT)''')
    connection.executemany('INSERT INTO orders VALUES(?,?,?,?,?,?)',[
        ('2026-01-03','华东','线上','c1',100.0,'paid'),
        ('2026-01-17','华东','门店','c2',200.0,'paid'),
        ('2026-02-04','华南','线上','c1',150.0,'paid'),
        ('2026-02-20','华东','线上','c3',50.0,'paid'),
    ])
    connection.commit();connection.close()


@pytest.fixture
def analytics_team(tmp_path,monkeypatch):
    monkeypatch.setenv('ESI_DATA_ROOTS',str(tmp_path))
    database=tmp_path/'sales.db';make_sales(database)
    admin=TestClient(app);sign_in(admin)
    response=admin.post('/api/data-sources',json={
        'name':'销售订单','kind':'sqlite','path':str(database),'description':'测试销售数据'})
    assert response.status_code==200,response.text
    source=response.json()['id']
    for metric in [
      {'name':'销售额','description':'订单金额求和','table_name':'orders','aggregation':'sum',
       'value_column':'amount','date_column':'order_date','dimensions':['region','channel']},
      {'name':'客户数','description':'购买客户去重数','table_name':'orders','aggregation':'count_distinct',
       'value_column':'customer_id','date_column':'order_date','dimensions':['region']},
    ]:
        assert admin.post(f'/api/data-sources/{source}/metrics',json=metric).status_code==200
    clients={}
    for username,role in [('analyst','analyst'),('reader','viewer')]:
        password='Initial-password-'+username+'-123'
        created=admin.post('/api/users',json={'username':username,'display_name':username,'password':password,'role':'member'})
        uid=created.json()['id']
        client=TestClient(app);login=sign_in(client,username,password)
        changed=client.post('/api/auth/password',json={'current_password':password,'new_password':'Changed-password-'+username+'-456'})
        client.headers['X-ESI-CSRF']=changed.json()['csrf']
        assert admin.post(f'/api/data-sources/{source}/members',json={'user_id':uid,'role':role}).status_code==200
        clients[username]=client
    return admin,clients,source,database


def test_data_source_permissions_and_schema(analytics_team,tmp_path):
    admin,clients,source,database=analytics_team
    analyst,reader=clients['analyst'],clients['reader']
    visible=analyst.get('/api/data-sources').json()[0]
    assert visible['path']=='由数据负责人管理'
    assert len(visible['metrics'])==2
    assert analyst.get(f'/api/data-sources/{source}/schema').status_code==200
    assert reader.get(f'/api/data-sources/{source}/schema').status_code==403
    assert analyst.post('/api/data-sources',json={'name':'x','kind':'sqlite','path':str(database),'description':''}).status_code==403
    assert reader.post('/api/analysis-jobs',json={'source_id':source,'title':'x','question':'x','report_kind':'report'}).status_code==403
    outside=tmp_path.parent/'outside-sales.db';make_sales(outside)
    denied=admin.post('/api/data-sources',json={'name':'outside','kind':'sqlite','path':str(outside),'description':''})
    assert denied.status_code==400


def test_analysis_plan_approval_execution_and_evidence(analytics_team,monkeypatch):
    admin,clients,source,database=analytics_team
    analyst=clients['analyst']
    created=analyst.post('/api/analysis-jobs',json={
        'source_id':source,'title':'月度销售复盘','question':'分析 2026 年前两个月各区域销售额和客户数',
        'report_kind':'strategy'})
    assert created.status_code==200,created.text
    jid=created.json()['id']
    metrics=admin.get('/api/data-sources').json()[0]['metrics']
    sales=next(item for item in metrics if item['name']=='销售额')
    customers=next(item for item in metrics if item['name']=='客户数')
    planning=iter([{'plan':'按月和区域分析销售额、客户数。','query':{
      'items':[{'metric_id':sales['id'],'dimensions':['region']},
               {'metric_id':customers['id'],'dimensions':['region']}],
      'grain':'month','start_date':'2026-01-01','end_date':'2026-02-28'},'_usage':{}}])
    monkeypatch.setattr(analysis_worker,'model',lambda messages:next(planning))
    job=core.query('SELECT * FROM analysis_jobs WHERE id=?',(jid,),True);job['status']='planning'
    core.execute("UPDATE analysis_jobs SET status='planning' WHERE id=?",(jid,))
    analysis_worker.run(job)
    detail=analyst.get('/api/analysis-jobs/'+jid).json()
    assert detail['status']=='awaiting_approval'
    assert detail['can_review'] is False
    assert analyst.post('/api/analysis-jobs/'+jid+'/approve',json={'version':1}).status_code==403
    assert admin.post('/api/analysis-jobs/'+jid+'/approve',json={'version':1}).status_code==200
    reporting=iter([{'done':True,'summary':'# 销售分析\n\n华东 1 月销售额 300，2 月销售额 50。','_usage':{}}])
    monkeypatch.setattr(analysis_worker,'model',lambda messages:next(reporting))
    job=core.query('SELECT * FROM analysis_jobs WHERE id=?',(jid,),True)
    analysis_worker.run(job)
    detail=analyst.get('/api/analysis-jobs/'+jid).json()
    assert detail['status']=='review'
    rows=detail['result']['rows']
    assert any(row['metric']=='销售额' and row['region']=='华东' and row['period']=='2026-01' and row['value']==300 for row in rows)
    assert any(row['metric']=='客户数' and row['region']=='华东' and row['period']=='2026-01' and row['value']==2 for row in rows)
    report=analyst.get('/api/analysis-jobs/'+jid+'/report')
    assert report.status_code==200
    assert '# 月度销售复盘' in report.text
    assert report.headers['content-type'].startswith('text/markdown')
    evidence=analyst.get('/api/analysis-jobs/'+jid+'/evidence')
    assert evidence.status_code==200
    assert 'metric' in evidence.text and '销售额' in evidence.text
    assert evidence.headers['content-type'].startswith('text/csv')
    detail['result']['rows'][0]['region']='=2+2'
    core.execute('UPDATE analysis_jobs SET result=? WHERE id=?',
                 (json.dumps(detail['result'],ensure_ascii=False),jid))
    assert "'=2+2" in analyst.get('/api/analysis-jobs/'+jid+'/evidence').text
    assert admin.post('/api/analysis-jobs/'+jid+'/complete',json={}).status_code==200
    assert sqlite3.connect(database).execute('SELECT COUNT(*) FROM orders').fetchone()[0]==4


def test_invalid_model_plan_cannot_execute(analytics_team,monkeypatch):
    admin,clients,source,_=analytics_team
    created=clients['analyst'].post('/api/analysis-jobs',json={
        'source_id':source,'title':'bad','question':'读取手机号明细','report_kind':'report'})
    jid=created.json()['id']
    monkeypatch.setattr(analysis_worker,'model',lambda messages:{'plan':'bad','query':{
      'items':[{'metric_id':'unknown','dimensions':['phone']}],'grain':'none'},'_usage':{}})
    job=core.query('SELECT * FROM analysis_jobs WHERE id=?',(jid,),True);job['status']='planning'
    core.execute("UPDATE analysis_jobs SET status='planning' WHERE id=?",(jid,))
    analysis_worker.run(job)
    assert core.query('SELECT status FROM analysis_jobs WHERE id=?',(jid,),True)['status']=='failed'


def test_csv_and_sqlite_ignore_invalid_numeric_values_consistently(tmp_path):
    import csv
    csv_path=tmp_path/'values.csv'
    with csv_path.open('w',newline='') as handle:
        writer=csv.writer(handle)
        writer.writerow(['region','value'])
        writer.writerows([['east','bad'],['east','2']])
    sqlite_path=tmp_path/'values.db'
    with sqlite3.connect(sqlite_path) as connection:
        connection.execute('CREATE TABLE data(region TEXT,value TEXT)')
        connection.executemany('INSERT INTO data VALUES(?,?)',
                               [('east','bad'),('east','2')])
    metric={'id':'average','name':'平均值','table_name':'data','aggregation':'avg',
            'value_column':'value','date_column':None}
    csv_rows=_csv_metric(csv_path,metric,['region'],'none',None,None)
    sqlite_rows=_sqlite_metric(sqlite_path,metric,['region'],'none',None,None)
    assert csv_rows[0]['value']==sqlite_rows[0]['value']==2.0
