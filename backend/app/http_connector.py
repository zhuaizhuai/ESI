"""Read-only, bounded HTTPS JSON connector for approved enterprise APIs."""
import json
import os
import re
from urllib.parse import urlsplit

import httpx

MAX_BYTES = 8_000_000
MAX_ROWS = 20_000
MAX_COLUMNS = 64
TOKEN_ENV = re.compile(r'^ESI_CONNECTOR_TOKEN_[A-Z0-9_]+$')


def validate_endpoint(url, auth_env=''):
    parsed = urlsplit(url)
    allowed = {host.strip().lower() for host in os.environ.get('ESI_CONNECTOR_HOSTS', '').split(',')
               if host.strip()}
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('连接器需要不含账号、密码和片段的 HTTPS 地址')
    if parsed.hostname.lower() not in allowed:
        raise ValueError('目标主机不在 ESI_CONNECTOR_HOSTS 白名单中')
    if auth_env and (not TOKEN_ENV.fullmatch(auth_env) or not os.environ.get(auth_env)):
        raise ValueError('连接器令牌环境变量无效或尚未配置')
    return url


def records(url, auth_env=''):
    validate_endpoint(url, auth_env or '')
    headers = {'Accept': 'application/json'}
    if auth_env:
        headers['Authorization'] = 'Bearer ' + os.environ[auth_env]
    chunks = []
    size = 0
    with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
        with client.stream('GET', url, headers=headers) as response:
            if response.status_code >= 300:
                raise ValueError('数据接口请求失败，HTTP ' + str(response.status_code))
            kind = response.headers.get('content-type', '').split(';')[0].lower()
            if kind != 'application/json' and not kind.endswith('+json'):
                raise ValueError('数据接口必须返回 JSON')
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError('数据接口响应超过 8 MB 上限')
                chunks.append(chunk)
    payload = json.loads(b''.join(chunks))
    if isinstance(payload, dict):
        payload = payload.get('data', payload.get('items'))
    if not isinstance(payload, list) or len(payload) > MAX_ROWS:
        raise ValueError('数据接口必须返回至多 20000 条 JSON 对象')
    columns = set()
    for row in payload:
        if not isinstance(row, dict) or any(not isinstance(key, str) or
            not isinstance(value, (str, int, float, bool, type(None))) or
            len(str(value)) > 4096 for key, value in row.items()):
            raise ValueError('数据接口只支持扁平 JSON 对象及简单值')
        columns.update(row)
        if len(columns) > MAX_COLUMNS:
            raise ValueError('数据接口字段超过 64 列上限')
    return payload
