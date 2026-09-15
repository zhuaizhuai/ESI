"""Planning and report generation for enterprise analysis jobs."""
import json
import time

from fastapi import HTTPException

from .analytics import execute, validate_spec
from .auth import require_data_source
from .core import connection, event, query
from .worker import model


def analysis_event(jid,message,kind='info',actor_id=None):
    with connection() as c:
        c.execute('INSERT INTO analysis_events(job_id,kind,message,created,actor_id) VALUES(?,?,?,?,?)',
                  (jid,kind,str(message)[:16000],time.time(),actor_id))


def update(jid,**values):
    with connection() as c:
        c.execute('UPDATE analysis_jobs SET '+','.join(key+'=?' for key in values)+
                  " WHERE id=? AND status NOT IN ('cancelled','interrupted')",(*values.values(),jid))


def active(jid):
    with connection() as c:
        job=c.execute('SELECT * FROM analysis_jobs WHERE id=?',(jid,)).fetchone()
        if not job or job['status'] in ('cancelled','interrupted'):
            raise InterruptedError('分析任务已取消或中断')
        creator=c.execute('SELECT * FROM users WHERE id=?',(job['created_by'],)).fetchone()
        try:
            if not creator: raise HTTPException(403,'分析任务缺少有效创建人')
            require_data_source(c,dict(creator),job['source_id'],'analyst')
            if job['status'] in ('queued','analyzing','reporting'):
                approver=c.execute('SELECT * FROM users WHERE id=?',(job['approved_by'],)).fetchone()
                if not approver or job['approved']!=job['version']:
                    raise HTTPException(403,'分析方案缺少有效审批')
                require_data_source(c,dict(approver),job['source_id'],'owner')
        except HTTPException as exc:
            c.execute("UPDATE analysis_jobs SET status='failed' WHERE id=? AND status NOT IN ('cancelled','interrupted')",(jid,))
            raise InterruptedError('分析授权已失效：'+str(exc.detail))
        return dict(job)


def _context(job,source,metrics):
    return {'question':job['question'],'report_kind':job['report_kind'],
            'source':{'name':source['name'],'kind':source['kind']},
            'metrics':[{'id':m['id'],'name':m['name'],'description':m['description'],
                        'aggregation':m['aggregation'],'date_column':m['date_column'],
                        'dimensions':json.loads(m['dimensions'])} for m in metrics]}


def plan(job,source,metrics):
    prompt='''你是企业经营分析规划助手。只根据提供的指标目录规划分析，不得要求读取原始明细、未知字段或任意 SQL。返回一个 JSON 对象：{"plan":"给负责人审阅的中文 Markdown，说明目标、指标、维度、时间范围、验证与局限","query":{"items":[{"metric_id":"指标ID","dimensions":["允许维度"]}],"grain":"none|day|month","start_date":"YYYY-MM-DD 或 null","end_date":"YYYY-MM-DD 或 null"}}。选择 1-6 个指标，每个最多 2 个维度。不能从问题推断出的时间范围使用 null。'''
    response=model([{'role':'system','content':prompt},{'role':'user','content':json.dumps(_context(job,source,metrics),ensure_ascii=False)}])
    response.pop('_usage',None)
    if not isinstance(response.get('plan'),str) or not isinstance(response.get('query'),dict):
        raise ValueError('模型没有返回有效的分析方案')
    spec=validate_spec(response['query'],metrics)
    return response['plan'],spec


def report(job,source,metrics,spec,rows):
    kind='战略建议' if job['report_kind']=='strategy' else '经营分析报告'
    prompt=f'''你是企业经营分析助手。根据已经由系统计算的聚合指标编写中文{kind}。数据行是唯一数值证据，严禁编造数字或声称因果关系已经得到证明。明确数据范围、指标口径、观察、风险和下一步。战略建议必须列出假设、至少两个方案、衡量指标和验证方法。返回 JSON：{{"done":true,"summary":"Markdown 报告"}}。'''
    evidence={'question':job['question'],'plan':job['plan'],'query':spec,
              'metrics':[{'name':m['name'],'description':m['description'],'aggregation':m['aggregation']} for m in metrics if any(i['metric_id']==m['id'] for i in spec['items'])],
              'rows':rows}
    response=model([{'role':'system','content':prompt},{'role':'user','content':json.dumps(evidence,ensure_ascii=False)}])
    response.pop('_usage',None)
    if response.get('done') is not True or not isinstance(response.get('summary'),str):
        raise ValueError('模型没有返回有效报告')
    return response['summary']


def run(job):
    jid=job['id'];planning=job['status']=='planning'
    try:
        active(jid)
        source=query('SELECT * FROM data_sources WHERE id=?',(job['source_id'],),True)
        metrics=query('SELECT * FROM metrics WHERE source_id=? ORDER BY created',(job['source_id'],))
        if not metrics: raise ValueError('数据源尚未配置指标')
        if planning:
            analysis_event(jid,'正在根据指标目录生成分析方案')
            text,spec=plan(job,source,metrics)
            active(jid)
            update(jid,status='awaiting_approval',plan=text,query_spec=json.dumps(spec,ensure_ascii=False),version=job['version']+1)
            analysis_event(jid,'分析方案已生成，等待负责人审批')
            return
        update(jid,status='analyzing');analysis_event(jid,'开始执行经过批准的指标计算')
        spec=json.loads(job['query_spec']);spec,rows=execute(source,metrics,spec)
        active(jid);update(jid,status='reporting');analysis_event(jid,f'完成 {len(rows)} 个聚合结果，正在生成报告')
        summary=report(job,source,metrics,spec,rows)
        active(jid)
        result={'report':summary,'query':spec,'rows':rows,'generated_at':time.time(),
                'source':source['name'],'row_count':len(rows)}
        update(jid,status='review',result=json.dumps(result,ensure_ascii=False))
        analysis_event(jid,'报告生成完成，等待负责人验收')
    except InterruptedError as exc:
        analysis_event(jid,str(exc),'error')
    except Exception as exc:
        update(jid,status='failed');analysis_event(jid,str(exc),'error')
