"""Planning and report generation for enterprise analysis jobs."""
import json
import time

from fastapi import HTTPException

from .analytics import execute_multi, validate_spec
from .core import connection, event, query
from .platform import job_source_ids, require_analysis_sources, sync_workflow
from .worker import model


def analysis_event(jid,message,kind='info',actor_id=None):
    with connection() as c:
        c.execute('INSERT INTO analysis_events(job_id,kind,message,created,actor_id) VALUES(?,?,?,?,?)',
                  (jid,kind,str(message)[:16000],time.time(),actor_id))


def update(jid,**values):
    with connection() as c:
        changed=c.execute('UPDATE analysis_jobs SET '+','.join(key+'=?' for key in values)+
                  " WHERE id=? AND status NOT IN ('cancelled','interrupted')",(*values.values(),jid))
        if changed.rowcount and 'status' in values:
            sync_workflow(c,'analysis',jid,values['status'])


def active(jid):
    with connection() as c:
        job=c.execute('SELECT * FROM analysis_jobs WHERE id=?',(jid,)).fetchone()
        if not job or job['status'] in ('cancelled','interrupted'):
            raise InterruptedError('分析任务已取消或中断')
        creator=c.execute('SELECT * FROM users WHERE id=?',(job['created_by'],)).fetchone()
        try:
            if not creator: raise HTTPException(403,'分析任务缺少有效创建人')
            require_analysis_sources(c,dict(creator),job,'analyst')
            if job['status'] in ('queued','analyzing','reporting'):
                approver=c.execute('SELECT * FROM users WHERE id=?',(job['approved_by'],)).fetchone()
                if not approver or job['approved']!=job['version']:
                    raise HTTPException(403,'分析方案缺少有效审批')
                require_analysis_sources(c,dict(approver),job,'owner')
        except HTTPException as exc:
            c.execute("UPDATE analysis_jobs SET status='failed' WHERE id=? AND status NOT IN ('cancelled','interrupted')",(jid,))
            sync_workflow(c,'analysis',jid,'failed')
            c.commit()
            raise InterruptedError('分析授权已失效：'+str(exc.detail))
        return dict(job)


def _context(job,sources,metrics):
    return {'question':job['question'],'report_kind':job['report_kind'],
            'sources':[{'id':s['id'],'name':s['name'],'kind':s['kind']} for s in sources],
            'metrics':[{'id':m['id'],'name':m['name'],'description':m['description'],
                        'source_id':m['source_id'],
                        'unified_name':m.get('term_name'),'unified_definition':m.get('term_definition'),
                        'unit':m.get('term_unit'),
                        'aggregation':m['aggregation'],'date_column':m['date_column'],
                        'dimensions':json.loads(m['dimensions'])} for m in metrics]}


def plan(job,sources,metrics):
    prompt='''你是企业经营分析规划助手。只根据提供的指标目录规划分析，不得要求读取原始明细、未知字段或任意 SQL。返回一个 JSON 对象：{"plan":"给负责人审阅的中文 Markdown，说明目标、指标、维度、时间范围、验证与局限","query":{"items":[{"metric_id":"指标ID","dimensions":["允许维度"]}],"grain":"none|day|month","start_date":"YYYY-MM-DD 或 null","end_date":"YYYY-MM-DD 或 null"}}。选择 1-6 个指标，每个最多 2 个维度。不能从问题推断出的时间范围使用 null。'''
    response=model([{'role':'system','content':prompt},{'role':'user','content':json.dumps(_context(job,sources,metrics),ensure_ascii=False)}])
    response.pop('_usage',None)
    if not isinstance(response.get('plan'),str) or not isinstance(response.get('query'),dict):
        raise ValueError('模型没有返回有效的分析方案')
    spec=validate_spec(response['query'],metrics)
    return response['plan'],spec


def report(job,sources,metrics,spec,rows):
    kind='战略建议' if job['report_kind']=='strategy' else '经营分析报告'
    prompt=f'''你是企业经营分析助手。根据已经由系统计算的聚合指标编写中文{kind}。数据行是唯一数值证据，严禁编造数字或声称因果关系已经得到证明。明确数据范围、指标口径、观察、风险和下一步。战略建议必须列出假设、至少两个方案、衡量指标和验证方法。返回 JSON：{{"done":true,"summary":"Markdown 报告","recommendations":[{{"title":"建议标题","action":"可执行动作","success_metric":"衡量效果的指标"}}]}}。建议最多 8 条，没有充分证据时返回空数组。'''
    evidence={'question':job['question'],'plan':job['plan'],'query':spec,
              'metrics':[{'name':m['name'],'description':m['description'],'aggregation':m['aggregation'],
                          'unified_name':m.get('term_name'),'unified_definition':m.get('term_definition'),
                          'unit':m.get('term_unit')} for m in metrics if any(i['metric_id']==m['id'] for i in spec['items'])],
              'rows':rows}
    response=model([{'role':'system','content':prompt},{'role':'user','content':json.dumps(evidence,ensure_ascii=False)}])
    response.pop('_usage',None)
    if response.get('done') is not True or not isinstance(response.get('summary'),str):
        raise ValueError('模型没有返回有效报告')
    recommendations=response.get('recommendations',[])
    if not isinstance(recommendations,list) or len(recommendations)>8:
        raise ValueError('模型返回的建议列表无效')
    cleaned=[]
    for item in recommendations:
        if not isinstance(item,dict) or any(not isinstance(item.get(key),str) or not item[key].strip()
           for key in ('title','action','success_metric')):
            raise ValueError('模型返回的建议缺少动作或衡量指标')
        cleaned.append({key:item[key][:2000] for key in ('title','action','success_metric')})
    return response['summary'],cleaned


def run(job):
    jid=job['id'];planning=job['status']=='planning'
    try:
        active(jid)
        with connection() as c:
            ids=job_source_ids(c,job)
            sources=[dict(c.execute('SELECT * FROM data_sources WHERE id=?',(sid,)).fetchone()) for sid in ids]
            metrics=[dict(row) for row in c.execute('''SELECT m.*,t.name AS term_name,
                t.definition AS term_definition,t.unit AS term_unit FROM metrics m
                LEFT JOIN metric_term_links l ON l.metric_id=m.id
                LEFT JOIN metric_terms t ON t.id=l.term_id WHERE m.source_id IN ('''+
                ','.join('?' for _ in ids)+') ORDER BY m.created',ids)]
        if not metrics: raise ValueError('数据源尚未配置指标')
        if planning:
            analysis_event(jid,'正在根据指标目录生成分析方案')
            text,spec=plan(job,sources,metrics)
            active(jid)
            update(jid,status='awaiting_approval',plan=text,query_spec=json.dumps(spec,ensure_ascii=False),version=job['version']+1)
            analysis_event(jid,'分析方案已生成，等待负责人审批')
            return
        update(jid,status='analyzing');analysis_event(jid,'开始执行经过批准的指标计算')
        spec=json.loads(job['query_spec']);spec,rows=execute_multi(sources,metrics,spec)
        active(jid);update(jid,status='reporting');analysis_event(jid,f'完成 {len(rows)} 个聚合结果，正在生成报告')
        summary,recommendations=report(job,sources,metrics,spec,rows)
        active(jid)
        result={'report':summary,'query':spec,'rows':rows,'generated_at':time.time(),
                'source':'、'.join(source['name'] for source in sources),'row_count':len(rows)}
        update(jid,status='review',result=json.dumps(result,ensure_ascii=False))
        with connection() as c:
            if c.execute("SELECT status FROM analysis_jobs WHERE id=?",(jid,)).fetchone()['status']=='review':
                import uuid
                c.executemany('''INSERT INTO recommendations
                  (id,job_id,title,action,success_metric,created,updated)
                  VALUES(?,?,?,?,?,?,?)''',[
                    (uuid.uuid4().hex[:12],jid,item['title'],item['action'],item['success_metric'],time.time(),time.time())
                    for item in recommendations])
        analysis_event(jid,'报告生成完成，等待负责人验收')
    except InterruptedError as exc:
        analysis_event(jid,str(exc),'error')
    except Exception as exc:
        update(jid,status='failed');analysis_event(jid,str(exc),'error')
