"""Read-only, bounded HTTPS JSON connector for approved enterprise APIs."""
import json
import math
import os
import re
import time
from urllib.parse import urlsplit, urljoin

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


def configuration(value=None):
    if isinstance(value, str):
        value = json.loads(value or '{}')
    if not isinstance(value or {}, dict):
        raise ValueError('连接器配置需要是对象')
    value = value or {}
    if set(value) - {'pagination', 'next_field', 'max_pages', 'retries'}:
        raise ValueError('存在不支持的连接器配置')
    result = {'pagination': value.get('pagination', 'none'),
              'next_field': value.get('next_field', 'next'),
              'max_pages': value.get('max_pages', 20), 'retries': value.get('retries', 1)}
    if result['pagination'] not in ('none', 'next_url'):
        raise ValueError('不支持的分页方式')
    if not isinstance(result['next_field'], str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', result['next_field']):
        raise ValueError('下一页字段名无效')
    for key, low, high in (('max_pages', 1, 50), ('retries', 0, 2)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError('连接器 '+key+' 超出范围')
    return result


def _page(client, url, headers, retries, remaining_bytes, deadline):
    for attempt in range(retries + 1):
        chunks = []
        size = 0
        if time.monotonic() >= deadline:
            raise ValueError('数据接口读取超过 60 秒，请缩小范围')
        try:
            with client.stream('GET', url, headers=headers,timeout=min(20,max(.1,deadline-time.monotonic()))) as response:
                if response.status_code in (429, 502, 503, 504) and attempt < retries:
                    time.sleep(.2 * (attempt + 1))
                    continue
                if response.status_code >= 300:
                    raise ValueError('数据接口请求失败，HTTP ' + str(response.status_code))
                kind = response.headers.get('content-type', '').split(';')[0].lower()
                if kind != 'application/json' and not kind.endswith('+json'):
                    raise ValueError('数据接口必须返回 JSON')
                for chunk in response.iter_bytes():
                    if time.monotonic() >= deadline:
                        raise ValueError('数据接口读取超过 60 秒，请缩小范围')
                    size += len(chunk)
                    if size > remaining_bytes:
                        raise ValueError('数据接口响应超过 8 MB 上限')
                    chunks.append(chunk)
            return json.loads(b''.join(chunks)), size
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            if attempt == retries:
                raise ValueError('数据接口连接超时或网络不可用') from exc
            time.sleep(.2 * (attempt + 1))
    raise ValueError('数据接口读取失败')


def records(url, auth_env='', connector_config=None):
    validate_endpoint(url, auth_env or '')
    config = configuration(connector_config)
    headers = {'Accept': 'application/json'}
    if auth_env:
        headers['Authorization'] = 'Bearer ' + os.environ[auth_env]
    size = 0
    rows = []
    original = urlsplit(url)
    origin = (original.scheme, original.hostname, original.port or 443)
    visited = set()
    deadline = time.monotonic() + 60
    with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
        for page in range(config['max_pages']):
            if url in visited:
                raise ValueError('数据接口分页出现循环')
            visited.add(url)
            payload, used = _page(client, url, headers, config['retries'], MAX_BYTES-size, deadline)
            size += used
            following = payload.get(config['next_field']) if isinstance(payload, dict) else None
            data = payload.get('data', payload.get('items')) if isinstance(payload, dict) else payload
            if not isinstance(data, list) or len(rows) + len(data) > MAX_ROWS:
                raise ValueError('数据接口必须返回合计至多 20000 条 JSON 对象')
            rows.extend(data)
            if config['pagination'] == 'none':
                if following:
                    raise ValueError('数据接口包含下一页，请启用分页以保证完整性')
                break
            if following is None or following == '':
                break
            if not isinstance(following, str):
                raise ValueError('下一页地址需要是字符串')
            next_url = urljoin(url, following)
            validate_endpoint(next_url, auth_env or '')
            parsed = urlsplit(next_url)
            if (parsed.scheme, parsed.hostname, parsed.port or 443) != origin:
                raise ValueError('下一页必须与原接口同源')
            if page + 1 >= config['max_pages']:
                raise ValueError('数据接口超过分页上限，拒绝生成不完整结果')
            url = next_url
    columns = set()
    for row in rows:
        if not isinstance(row, dict) or any(not isinstance(key, str) or
            not isinstance(value, (str, int, float, bool, type(None))) or
            len(str(value)) > 4096 or isinstance(value,float) and not math.isfinite(value)
            for key, value in row.items()):
            raise ValueError('数据接口只支持扁平 JSON 对象及简单值')
        columns.update(row)
        if len(columns) > MAX_COLUMNS:
            raise ValueError('数据接口字段超过 64 列上限')
    return rows
