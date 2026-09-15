"""Read-only enterprise data connectors and deterministic metric execution."""
import csv
import json
import os
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path

from .core import DATA

AGGREGATIONS = {'sum', 'avg', 'count', 'count_distinct', 'min', 'max'}
GRAINS = {'none', 'day', 'month'}


def data_roots():
    configured = os.environ.get('ESI_DATA_ROOTS', str(DATA))
    return [Path(item.strip()).expanduser().resolve() for item in configured.split(',') if item.strip()]


def source_path(value):
    path = Path(value).expanduser().resolve()
    if not any(path == root or root in path.parents for root in data_roots()):
        raise ValueError('数据文件不在 ESI_DATA_ROOTS 允许的目录中')
    if not path.is_file() or path.is_symlink():
        raise ValueError('数据文件不存在或不允许使用符号链接')
    if any(part.startswith('.') for part in path.parts):
        raise ValueError('不允许读取隐藏目录或隐藏文件')
    return path


def quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def schema(kind, value):
    path = source_path(value)
    if kind == 'csv':
        if path.suffix.lower() != '.csv':
            raise ValueError('CSV 数据源必须使用 .csv 文件')
        with path.open(newline='', encoding='utf-8-sig') as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
        if not header or any(not column.strip() for column in header):
            raise ValueError('CSV 缺少有效表头')
        if len(set(header)) != len(header):
            raise ValueError('CSV 表头不能重名')
        return {'data': header}
    if kind == 'sqlite':
        if path.suffix.lower() not in {'.db', '.sqlite', '.sqlite3'}:
            raise ValueError('SQLite 数据源必须使用 .db、.sqlite 或 .sqlite3 文件')
        connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
        try:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            result = {}
            for table in tables:
                result[table] = [row[1] for row in connection.execute('PRAGMA table_info('+quote(table)+')')]
            if not result:
                raise ValueError('SQLite 数据库中没有可用的数据表')
            return result
        finally:
            connection.close()
    raise ValueError('不支持的数据源类型')


def validate_metric(source, metric):
    available = schema(source['kind'], source['path'])
    table = metric['table_name']
    if table not in available:
        raise ValueError('指标数据表不存在')
    columns = set(available[table])
    aggregation = metric['aggregation']
    if aggregation not in AGGREGATIONS:
        raise ValueError('不支持的聚合方式')
    value = metric.get('value_column')
    if aggregation != 'count' and value not in columns:
        raise ValueError('指标数值字段不存在')
    date_column = metric.get('date_column')
    if date_column and date_column not in columns:
        raise ValueError('指标日期字段不存在')
    dimensions = metric.get('dimensions', [])
    if len(dimensions) > 8 or any(item not in columns for item in dimensions):
        raise ValueError('指标维度字段不存在或数量过多')


def validate_spec(spec, metrics):
    known = {item['id']: item for item in metrics}
    items = spec.get('items')
    if not isinstance(items, list) or not 1 <= len(items) <= 6:
        raise ValueError('分析计划需要选择 1–6 个指标')
    grain = spec.get('grain', 'none')
    if grain not in GRAINS:
        raise ValueError('不支持的时间粒度')
    start, end = spec.get('start_date'), spec.get('end_date')
    for value in (start, end):
        if value:
            date.fromisoformat(value)
    if start and end and start > end:
        raise ValueError('开始日期不能晚于结束日期')
    normalized=[]
    for item in items:
        metric = known.get(item.get('metric_id'))
        if not metric:
            raise ValueError('分析计划引用了未知指标')
        dimensions=item.get('dimensions', [])
        allowed=json.loads(metric['dimensions']) if isinstance(metric['dimensions'], str) else metric['dimensions']
        if not isinstance(dimensions, list) or len(dimensions)>2 or any(x not in allowed for x in dimensions):
            raise ValueError('分析计划使用了未授权维度')
        if (grain != 'none' or start or end) and not metric.get('date_column'):
            raise ValueError('所选指标没有日期字段，不能使用时间范围或粒度')
        normalized.append({'metric_id':metric['id'],'dimensions':dimensions})
    return {'items':normalized,'grain':grain,'start_date':start,'end_date':end}


def _grain(value, grain):
    if grain == 'day':
        return (value or '')[:10]
    if grain == 'month':
        return (value or '')[:7]
    return None


def _finish(state, aggregation):
    if aggregation == 'avg':
        return state[0] / state[1] if state[1] else None
    if aggregation == 'count_distinct':
        return len(state)
    return state


def _csv_metric(path, metric, dimensions, grain, start, end):
    groups={}
    with path.open(newline='',encoding='utf-8-sig') as handle:
        for row in csv.DictReader(handle):
            current=row.get(metric.get('date_column')) if metric.get('date_column') else None
            if start and (not current or current[:10] < start): continue
            if end and (not current or current[:10] > end): continue
            key=tuple(row.get(item,'') for item in dimensions)
            if grain!='none': key += (_grain(current,grain),)
            aggregation=metric['aggregation']; raw=row.get(metric.get('value_column'))
            if aggregation=='count': groups[key]=groups.get(key,0)+1
            elif aggregation=='count_distinct': groups.setdefault(key,set()).add(raw)
            else:
                try: number=float(raw)
                except (TypeError,ValueError): continue
                if aggregation=='sum': groups[key]=groups.get(key,0.0)+number
                elif aggregation=='avg':
                    total,count=groups.get(key,(0.0,0));groups[key]=(total+number,count+1)
                elif aggregation=='min': groups[key]=number if key not in groups else min(groups[key],number)
                elif aggregation=='max': groups[key]=number if key not in groups else max(groups[key],number)
    return _rows(groups,metric,dimensions,grain)


def _sqlite_metric(path, metric, dimensions, grain, start, end):
    selected=[quote(item) for item in dimensions]
    group=[quote(item) for item in dimensions]
    labels=list(dimensions)
    if grain!='none':
        length=10 if grain=='day' else 7
        expression=f'substr({quote(metric["date_column"])},1,{length})'
        selected.append(expression+' AS period');group.append(expression);labels.append('period')
    aggregation=metric['aggregation']
    if aggregation=='count': measure='COUNT(*)'
    elif aggregation=='count_distinct': measure='COUNT(DISTINCT '+quote(metric['value_column'])+')'
    else: measure=aggregation.upper()+'(CAST('+quote(metric['value_column'])+' AS REAL))'
    sql='SELECT '+(','.join(selected)+',' if selected else '')+measure+' AS value FROM '+quote(metric['table_name'])
    where=[];params=[]
    if start: where.append(quote(metric['date_column'])+' >= ?');params.append(start)
    if end: where.append(quote(metric['date_column'])+' <= ?');params.append(end+'T23:59:59')
    if where: sql+=' WHERE '+' AND '.join(where)
    if group: sql+=' GROUP BY '+','.join(group)
    sql+=' ORDER BY '+(','.join(group) if group else 'value DESC')+' LIMIT 200'
    connection=sqlite3.connect(f'file:{path}?mode=ro',uri=True,timeout=10)
    try:
        result=[]
        for values in connection.execute(sql,params).fetchall():
            row={label:values[index] for index,label in enumerate(labels)}
            row.update(metric_id=metric['id'],metric=metric['name'],value=values[-1])
            result.append(row)
        return result
    finally: connection.close()


def _rows(groups,metric,dimensions,grain):
    labels=list(dimensions)+(['period'] if grain!='none' else [])
    rows=[]
    for key,state in sorted(groups.items(),key=lambda item:item[0])[:200]:
        row={label:key[index] for index,label in enumerate(labels)}
        row.update(metric_id=metric['id'],metric=metric['name'],value=_finish(state,metric['aggregation']))
        rows.append(row)
    return rows


def execute(source, metrics, spec):
    normalized=validate_spec(spec,metrics)
    known={item['id']:item for item in metrics}
    path=source_path(source['path'])
    rows=[]
    for item in normalized['items']:
        metric=known[item['metric_id']]
        if source['kind']=='sqlite':
            current=_sqlite_metric(path,metric,item['dimensions'],normalized['grain'],normalized['start_date'],normalized['end_date'])
        else:
            current=_csv_metric(path,metric,item['dimensions'],normalized['grain'],normalized['start_date'],normalized['end_date'])
        rows.extend(current)
        if len(rows)>500:
            raise ValueError('分析结果超过 500 个聚合分组，请缩小指标或维度范围')
    return normalized,rows
