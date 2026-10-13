"""Connector configuration and observable source health without long write transactions."""
import json
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import audit, current_user, require_data_source
from .core import connection
from .http_connector import configuration

router=APIRouter(prefix='/api/data-sources')


def init_sources():
    with connection() as c:
        columns={r['name'] for r in c.execute('PRAGMA table_info(data_sources)')}
        for name in ('connector_config','connector_health'):
            if name not in columns:
                c.execute('ALTER TABLE data_sources ADD COLUMN '+name+" TEXT NOT NULL DEFAULT '{}'")


def probe(source):
    from .analytics import schema
    try:
        available=schema(source['kind'],source['path'],source.get('auth_env') or '',source.get('connector_config'))
        return available,{'status':'ok','checked_at':time.time()}
    except Exception as exc:
        return None,{'status':'error','checked_at':time.time(),'message':str(exc)[:1000]}


@router.post('/{sid}/health')
def check_health(sid:str,user=Depends(current_user)):
    with connection() as c:
        source=require_data_source(c,user,sid,'owner')
    _,health=probe(source)
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        fresh=require_data_source(c,user,sid,'owner')
        if any(fresh.get(k)!=source.get(k) for k in ('kind','path','auth_env','connector_config')):
            raise HTTPException(409,'检查期间数据源配置已变化')
        c.execute('UPDATE data_sources SET connector_health=? WHERE id=?',(json.dumps(health,ensure_ascii=False),sid))
        audit(c,user['id'],'source_health_checked',sid,health['status'])
    return health


class ConnectorConfiguration(BaseModel):
    connector_config: dict=Field(default_factory=dict)


@router.post('/{sid}/connector-config')
def configure_connector(sid:str,data:ConnectorConfiguration,user=Depends(current_user)):
    with connection() as c:
        source=require_data_source(c,user,sid,'owner')
    if source['kind']!='http_json':
        raise HTTPException(400,'仅 HTTPS JSON 接口支持连接器配置')
    try:
        config=configuration(data.connector_config)
    except (ValueError,TypeError) as exc:
        raise HTTPException(400,str(exc)) from exc
    candidate={**source,'connector_config':json.dumps(config)}
    _,health=probe(candidate)
    if health['status']!='ok':
        raise HTTPException(400,'连接器配置无效：'+health['message'])
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        fresh=require_data_source(c,user,sid,'owner')
        if any(fresh.get(k)!=source.get(k) for k in ('kind','path','auth_env','connector_config')):
            raise HTTPException(409,'检查期间配置已变化，请刷新')
        c.execute('UPDATE data_sources SET connector_config=?,connector_health=? WHERE id=?',
                  (json.dumps(config),json.dumps(health),sid))
        audit(c,user['id'],'connector_configured',sid,json.dumps(config))
    return {'ok':True,'connector_config':config,'connector_health':health}


init_sources()
