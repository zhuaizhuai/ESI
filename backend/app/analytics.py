"""Read-only enterprise data connectors and deterministic metric execution."""
import csv
import hashlib
import json
import math
import os
import sqlite3
import time
from datetime import date
from pathlib import Path

from .core import DATA
from .http_connector import records as http_records

AGGREGATIONS = {'sum', 'avg', 'count', 'count_distinct', 'min', 'max'}
GRAINS = {'none', 'day', 'month'}
RESERVED = {'period', 'value', 'metric_id', 'metric', 'source_id', 'source_name', 'evidence_id'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def data_roots():
    configured = os.environ.get('ESI_DATA_ROOTS', str(DATA))
    return [Path(item.strip()).expanduser().resolve() for item in configured.split(',') if item.strip()]


def source_path(value):
    raw = Path(value).expanduser().absolute()
    if raw.is_symlink() or any(parent.is_symlink() for parent in raw.parents):
        raise ValueError('不允许读取符号链接')
    path = raw.resolve()
    if not any(path == root or root in path.parents for root in data_roots()):
        raise ValueError('数据文件不在 ESI_DATA_ROOTS 允许的目录中')
    if not path.is_file() or path.is_symlink():
        raise ValueError('数据文件不存在或不允许使用符号链接')
    if any(part.startswith('.') for part in path.parts):
        raise ValueError('不允许读取隐藏目录或隐藏文件')
    return path


def quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def schema(kind, value, auth_env='', connector_config=None):
    if kind == 'http_json':
        rows = http_records(value, auth_env, connector_config)
        columns = list(dict.fromkeys(key for row in rows for key in row))
        if not columns:
            raise ValueError('数据接口未返回可识别字段')
        return {'data': columns}
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
    available = schema(source['kind'], source['path'], source.get('auth_env') or '', source.get('connector_config'))
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
    if not isinstance(spec, dict):
        raise ValueError('分析计划必须为对象')
    known = {item['id']: item for item in metrics}
    items = spec.get('items')
    if not isinstance(items, list) or not 1 <= len(items) <= 6:
        raise ValueError('分析计划需要选择 1–6 个指标')
    grain = spec.get('grain', 'none')
    if not isinstance(grain,str) or grain not in GRAINS:
        raise ValueError('不支持的时间粒度')
    start, end = spec.get('start_date'), spec.get('end_date')
    for value in (start, end):
        if value is not None and value!='':
            if not isinstance(value, str) or len(value) != 10:
                raise ValueError('日期必须为 YYYY-MM-DD')
            date.fromisoformat(value)
    if start and end and start > end:
        raise ValueError('开始日期不能晚于结束日期')
    normalized=[]
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('metric_id'), str):
            raise ValueError('指标选择必须包含有效 metric_id')
        metric = known.get(item.get('metric_id'))
        if not metric:
            raise ValueError('分析计划引用了未知指标')
        dimensions=item.get('dimensions', [])
        allowed=json.loads(metric['dimensions']) if isinstance(metric['dimensions'], str) else metric['dimensions']
        if not isinstance(dimensions, list) or len(dimensions)>2 or any(not isinstance(x,str) or x not in allowed or x in RESERVED for x in dimensions) or len(set(dimensions)) != len(dimensions):
            raise ValueError('分析计划使用了未授权维度')
        aliases=item.get('dimension_aliases', {})
        if not isinstance(aliases, dict) or any(k not in dimensions or not isinstance(v,str) or not v.strip() or len(v)>64 or v in RESERVED for k,v in aliases.items()):
            raise ValueError('维度映射必须引用选中维度及有效公共名称')
        labels=[aliases.get(d,d) for d in dimensions]
        if len(set(labels)) != len(labels):
            raise ValueError('公共维度名称不能重复')
        if (grain != 'none' or start or end) and not metric.get('date_column'):
            raise ValueError('所选指标没有日期字段，不能使用时间范围或粒度')
        normalized.append({'metric_id':metric['id'],'dimensions':dimensions, 'dimension_aliases':aliases})
    if len({i['metric_id'] for i in normalized}) != len(normalized):
        raise ValueError('指标不能重复选择')
    selected={i['metric_id']:i for i in normalized}
    comparisons=spec.get('comparisons',[]); scenarios=spec.get('scenarios',[])
    if not isinstance(comparisons,list) or len(comparisons)>6 or not isinstance(scenarios,list) or len(scenarios)>6:
        raise ValueError('比较和情景各最多 6 项')
    ids=set()
    for item in comparisons+scenarios:
        if not isinstance(item,dict) or not isinstance(item.get('id'),str) or not item['id'] or len(item['id'])>64 or item['id'] in ids or not isinstance(item.get('name'),str) or not item['name'].strip() or len(item['name'])>160:
            raise ValueError('计算需要唯一 ID 和名称')
        ids.add(item['id'])
    cleaned=[]
    for item in comparisons:
        left=item.get('left_metric_id'); right=item.get('right_metric_id'); operation=item.get('operation')
        if not isinstance(left,str) or not isinstance(right,str) or not isinstance(operation,str) or left not in selected or right not in selected or left==right or operation not in {'difference','ratio','percentage_change'}:
            raise ValueError('比较必须引用两个不同的选中指标和允许运算')
        dims=item.get('dimensions',[])
        common=[{i['dimension_aliases'].get(d,d) for d in i['dimensions']} for i in (selected[left],selected[right])]
        if not isinstance(dims,list) or any(not isinstance(d,str) for d in dims) or len(set(dims))!=len(dims) or set(dims)!=common[0] or set(dims)!=common[1]:
            raise ValueError('比较需要双方完全一致的公共维度，不能隐式汇总')
        if operation in {'difference','percentage_change'} and (not known[left].get('term_unit') or known[left].get('term_unit')!=known[right].get('term_unit')):
            raise ValueError('差值和变化率比较需要明确且一致的单位')
        cleaned.append({**{k:item[k] for k in ('id','name','operation','left_metric_id','right_metric_id')},'dimensions':dims})
    clean_scenarios=[]
    for item in scenarios:
        change=item.get('change_percent')
        if not isinstance(item.get('metric_id'),str) or item['metric_id'] not in selected or isinstance(change,bool) or not isinstance(change,(int,float)) or not math.isfinite(change) or not -100<=change<=1000:
            raise ValueError('情景需要选中指标及 -100 至 1000 的变化百分比')
        clean_scenarios.append({k:item[k] for k in ('id','name','metric_id','change_percent')})
    return {'items':normalized,'grain':grain,'start_date':start,'end_date':end,
            'comparisons':cleaned,'scenarios':clean_scenarios}


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


def _number(raw):
    try:
        value=float(raw)
        return value if math.isfinite(value) else None
    except (TypeError,ValueError):
        return None


def _date_value(raw):
    if raw is None:
        return None
    text=str(raw)
    try:
        return date.fromisoformat(text[:10]).isoformat() if len(text)>=10 else None
    except ValueError:
        return None


def _typed(value):
    """Retain zero, boolean and null as different keys, without coercing values."""
    return (type(value).__name__, value)


def _aggregate(records, metric, dimensions, grain, start, end, quality=None):
    quality=quality if quality is not None else {}
    quality.update(scanned_rows=0,included_rows=0,filtered_rows=0,invalid_numeric_rows=0,
                   invalid_date_rows=0,null_dimension_rows=0,complete=True)
    groups={}; started=time.monotonic(); fingerprint=hashlib.sha256()
    for index,row in enumerate(records,1):
        if None in row:
            raise ValueError('CSV 记录列数超过表头，请修复数据文件')
        if index>1_000_000 or (index%1000==0 and time.monotonic()-started>30):
            raise ValueError('指标计算超过 100 万行或 30 秒上限，请缩小范围')
        quality['scanned_rows']+=1
        fingerprint.update(json.dumps(row,sort_keys=True,ensure_ascii=False,default=str).encode())
        fingerprint.update(b'\n')
        current=_date_value(row.get(metric.get('date_column'))) if metric.get('date_column') else None
        if grain!='none' or start or end:
            if current is None:
                quality['invalid_date_rows']+=1
                continue
            if start and current<start or end and current>end:
                quality['filtered_rows']+=1
                continue
        values=tuple(row.get(d) for d in dimensions)
        if any(v is None for v in values): quality['null_dimension_rows']+=1
        key=tuple(_typed(v) for v in values)
        if grain!='none': key+=(_typed(_grain(current,grain)),)
        aggregation=metric['aggregation']; raw=row.get(metric.get('value_column'))
        if aggregation not in {'count','count_distinct'}:
            number=_number(raw)
            if number is None:
                quality['invalid_numeric_rows']+=1
                continue
        if aggregation=='count_distinct' and raw is None: continue
        if key not in groups and len(groups)>=200:
            raise ValueError('指标分组超过 200 个，请缩小范围；未返回截断结果')
        quality['included_rows']+=1
        if aggregation=='count': groups[key]=groups.get(key,0)+1
        elif aggregation=='count_distinct':
            distinct=groups.setdefault(key,set());distinct.add(_typed(raw))
            if len(distinct)>100_000: raise ValueError('去重值超过 100000 个，请缩小范围')
        elif aggregation=='sum': groups[key]=groups.get(key,0.0)+number
        elif aggregation=='avg':
            total,count=groups.get(key,(0.0,0));groups[key]=(total+number,count+1)
        elif aggregation=='min': groups[key]=number if key not in groups else min(groups[key],number)
        elif aggregation=='max': groups[key]=number if key not in groups else max(groups[key],number)
        else: raise ValueError('不支持的聚合方式')
    quality['input_fingerprint']=fingerprint.hexdigest()
    if not groups and not dimensions and grain=='none' and metric['aggregation'] in {'count','count_distinct'}:
        groups[()]=0 if metric['aggregation']=='count' else set()
    quality['group_count']=len(groups)
    return _rows(groups,metric,dimensions,grain)


def _csv_metric(path,metric,dimensions,grain,start,end,quality=None):
    with path.open(newline='',encoding='utf-8-sig') as handle:
        reader=csv.DictReader(handle)
        required=set(dimensions)|{metric.get('date_column')}
        if metric['aggregation']!='count': required.add(metric.get('value_column'))
        required.discard(None);required.discard('')
        if not required.issubset(reader.fieldnames or []):
            raise ValueError('数据源字段已变更，指标字段缺失')
        return _aggregate(reader,metric,dimensions,grain,start,end,quality)


def _http_metric(rows,metric,dimensions,grain,start,end,quality=None):
    if rows:
        required=set(dimensions)|{metric.get('date_column')}
        if metric['aggregation']!='count': required.add(metric.get('value_column'))
        required.discard(None);required.discard('')
        if not required.issubset({key for row in rows for key in row}):
            raise ValueError('数据源字段已变更，指标字段缺失')
    return _aggregate(rows,metric,dimensions,grain,start,end,quality)


def _sqlite_metric(path,metric,dimensions,grain,start,end,quality=None):
    value_column=metric.get('value_column') if metric['aggregation']!='count' else None
    columns=list(dict.fromkeys(dimensions+[v for v in (value_column,metric.get('date_column')) if v]))
    sql='SELECT '+(','.join(quote(v) for v in columns) if columns else '1 AS esi_row')+' FROM '+quote(metric['table_name'])
    connection=sqlite3.connect(f'file:{path}?mode=ro',uri=True,timeout=10)
    connection.row_factory=sqlite3.Row
    try:
        actual={r['name'] for r in connection.execute('PRAGMA table_info('+quote(metric['table_name'])+')')}
        if not actual or not set(columns).issubset(actual):
            raise ValueError('数据源字段已变更，指标字段缺失')
        started=time.monotonic()
        connection.set_progress_handler(lambda:int(time.monotonic()-started>30),10_000)
        return _aggregate((dict(r) for r in connection.execute(sql)),metric,dimensions,grain,start,end,quality)
    finally:
        connection.close()


def _rows(groups,metric,dimensions,grain):
    if len(groups)>200: raise ValueError('指标分组超过 200 个，请缩小范围')
    labels=list(dimensions)+(['period'] if grain!='none' else [])
    rows=[]
    for key,state in sorted(groups.items(),key=lambda item:repr(item[0])):
        row={label:key[index][1] for index,label in enumerate(labels)}
        row.update(metric_id=metric['id'],metric=metric['name'],value=_finish(state,metric['aggregation']))
        rows.append(row)
    return rows


def execute(source,metrics,spec,remote_rows=None,quality=None):
    normalized=validate_spec(spec,metrics)
    known={item['id']:item for item in metrics}
    path=source_path(source['path']) if source['kind']!='http_json' else None
    if source['kind']=='http_json' and remote_rows is None:
        remote_rows=http_records(source['path'],source.get('auth_env') or '',source.get('connector_config') or '{}')
    rows=[]
    for item in normalized['items']:
        metric=known[item['metric_id']]; q={}
        args=(metric,item['dimensions'],normalized['grain'],normalized['start_date'],normalized['end_date'],q)
        if source['kind']=='sqlite': current=_sqlite_metric(path,*args)
        elif source['kind']=='http_json': current=_http_metric(remote_rows,*args)
        else: current=_csv_metric(path,*args)
        for row in current:
            renamed={item['dimension_aliases'].get(k,k):v for k,v in row.items()}
            row.clear();row.update(renamed)
        if quality is not None: quality[metric['id']]=q
        rows.extend(current)
        if len(rows)>500: raise ValueError('分析结果超过 500 个分组，请缩小范围')
    return normalized,rows


def compute_calculations(spec,rows):
    results=[]
    for item in spec['comparisons']:
        dims=item['dimensions']+(['period'] if spec['grain']!='none' else [])
        indexes=[]
        for mid in (item['left_metric_id'],item['right_metric_id']):
            indexes.append({tuple(_typed(r.get(d)) for d in dims):r for r in rows if r['metric_id']==mid})
        for key in sorted(indexes[0].keys()|indexes[1].keys(),key=repr):
            left=indexes[0].get(key);right=indexes[1].get(key)
            result={'id':item['id'],'name':item['name'],'kind':'comparison','operation':item['operation'],
                    'dimensions':{d:key[n][1] for n,d in enumerate(dims)},'value':None,
                    'evidence_ids':[r['evidence_id'] for r in (left,right) if r]}
            if left is None or right is None: result['status']='missing_group'
            elif item['operation']!='difference' and right['value']==0: result['status']='zero_denominator'
            else:
                a=left['value'];b=right['value']; op=item['operation']
                result['value']=a-b if op=='difference' else a/b if op=='ratio' else (a-b)/abs(b)*100
                result['status']='computed'
            result['evidence_id']='calc-'+digest(result)[:20];results.append(result)
    for item in spec['scenarios']:
        for row in rows:
            if row['metric_id']!=item['metric_id']: continue
            result={'id':item['id'],'name':item['name'],'kind':'scenario','status':'assumption',
                    'baseline':row['value'],'change_percent':item['change_percent'],
                    'value':row['value']*(1+item['change_percent']/100),
                    'dimensions':{k:v for k,v in row.items() if k not in RESERVED},
                    'evidence_ids':[row['evidence_id']],
                    'limitation':'固定比例算术情景，不是预测或因果估计'}
            result['evidence_id']='calc-'+digest(result)[:20];results.append(result)
    if len(results)>500: raise ValueError('衍生计算超过 500 个结果，请缩小范围')
    return results


def execute_with_evidence(sources,metrics,spec):
    normalized=validate_spec(spec,metrics)
    known={m['id']:m for m in metrics};by_id={s['id']:s for s in sources}
    rows=[];quality={};source_evidence=[]
    for sid in dict.fromkeys(known[i['metric_id']]['source_id'] for i in normalized['items']):
        source=by_id.get(sid)
        if not source: raise ValueError('指标不属于本任务已授权的数据源')
        started=time.time()
        remote=http_records(source['path'],source.get('auth_env') or '',source.get('connector_config') or '{}') if source['kind']=='http_json' else None
        for item in normalized['items']:
            metric=known[item['metric_id']]
            if metric['source_id']!=sid: continue
            single={**normalized,'items':[item],'comparisons':[],'scenarios':[]}
            _,current=execute(source,[metric],single,remote,quality)
            for row in current:
                row.update(source_id=sid,source_name=source['name'])
                row['evidence_id']='metric-'+digest({'row':row,'input':quality[metric['id']]['input_fingerprint']})[:20]
            rows.extend(current)
            if len(rows)>500: raise ValueError('分析结果超过 500 个分组，请缩小范围')
        source_evidence.append({'source_id':sid,'source_name':source['name'],'kind':source['kind'],
                                'started_at':started,'collected_at':time.time(),
                                'complete':True,'configuration_hash':source_configuration_hash(source),
                                'data_fingerprint':digest({mid:q['input_fingerprint'] for mid,q in quality.items() if known[mid]['source_id']==sid})})
    definitions=[known[i['metric_id']] for i in normalized['items']]
    evidence={'sources':source_evidence,'metrics':definitions,'quality':quality,
              'calculations':compute_calculations(normalized,rows),
              'definition_hash':digest(definitions),'complete':True,
              'limitations':['跨源采集非同一时点事务；采集时间和逐指标输入指纹用于追溯',
                             '日期使用记录中的 ISO 日期部分，未进行时区转换',
                             '公共维度只映射名称，保留原值类型，不自动归并业务实体或转换单位',
                             '缺失/非法数值不计入数值聚合，见质量计数；完整表示未截断而非业务数据无缺失']}
    return normalized,rows,evidence


def source_configuration_hash(source):
    return digest({key:source.get(key) for key in ('id','kind','path','auth_env','connector_config')})


def execute_multi(sources,metrics,spec):
    normalized,rows,_=execute_with_evidence(sources,metrics,spec)
    return normalized,rows
