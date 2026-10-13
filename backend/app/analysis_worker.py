"""Planning and report generation for enterprise analysis jobs."""
import json
import time

from fastapi import HTTPException

from .analytics import execute_with_evidence, validate_spec
from .analysis import check_approval_snapshot, load_catalog
from .core import connection
from .platform import require_analysis_sources, sync_workflow
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
                from .orchestration import require_execution_approvals
                require_execution_approvals(c,'analysis',dict(job))
        except HTTPException as exc:
            changed=c.execute("UPDATE analysis_jobs SET status='failed' WHERE id=? AND status NOT IN ('cancelled','interrupted')",(jid,))
            if changed.rowcount:
                sync_workflow(c,'analysis',jid,'failed')
            c.commit()
            raise InterruptedError('分析授权已失效：'+str(exc.detail))
        return dict(job)


def _context(job,sources,metrics):
    return {'question':job['question'],'report_kind':job['report_kind'],
            'brief':json.loads(job.get('brief') or '{}'),
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


def report(job,sources,metrics,spec,rows,evidence=None):
    kind='战略建议' if job['report_kind']=='strategy' else '经营分析报告'
    prompt=f'''你是企业经营分析助手。根据系统聚合指标编写中文{kind}。数据行和calculations是唯一数值证据，严禁编造数字或把相关性当已证实因果。报告注明数据范围、质量缺口、口径、风险和下一步。情景计算仅是固定比例假设，不是预测。战略建议列明假设、至少两个方案、成本约束、衡量指标和验证方法。返回 JSON：{{"done":true,"summary":"Markdown 报告","claims":[{{"text":"观察结论","evidence_ids":["已有证据ID"],"kind":"observation|hypothesis|scenario"}}],"recommendations":[{{"title":"建议标题","action":"可执行动作","success_metric":"衡量效果的指标"}}]}}。每条claim引用至少一个已有evidence_id，最多30条。建议最多8条，证据不足返回空数组。claims引用验证不能代替人工核对正文数值与推理。'''
    payload={'question':job['question'],'plan':job['plan'],'query':spec,
              'brief':json.loads(job.get('brief') or '{}'),
              'metrics':[{'name':m['name'],'description':m['description'],'aggregation':m['aggregation'],
                          'unified_name':m.get('term_name'),'unified_definition':m.get('term_definition'),
                          'unit':m.get('term_unit')} for m in metrics if any(i['metric_id']==m['id'] for i in spec['items'])],
              'rows':rows,'evidence':evidence or {}}
    response=model([{'role':'system','content':prompt},{'role':'user','content':json.dumps(payload,ensure_ascii=False)}])
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
    claims=response.get('claims')
    if claims is None:
        return response['summary']+'\n\n> 模型文本未提供结构化证据引用，需人工审核。',cleaned,[], 'legacy_unverified'
    if not isinstance(claims,list) or not 1<=len(claims)<=30:
        raise ValueError('报告必须提供 1–30 条可追溯结论')
    valid={r['evidence_id'] for r in rows}
    valid.update(r['evidence_id'] for r in (evidence or {}).get('calculations',[]))
    for claim in claims:
        if not isinstance(claim,dict) or not isinstance(claim.get('text'),str) or not claim['text'].strip() or len(claim['text'])>4000 or claim.get('kind') not in {'observation','hypothesis','scenario'}:
            raise ValueError('报告结论格式无效')
        references=claim.get('evidence_ids')
        if not isinstance(references,list) or not 1<=len(references)<=20 or any(not isinstance(r,str) or r not in valid for r in references):
            raise ValueError('报告引用未知或缺失证据，未接受该报告')
    return response['summary'],cleaned,claims,'references_validated_content_requires_review'


def run(job):
    jid=job['id'];planning=job['status']=='planning'
    try:
        active(jid)
        with connection() as c:
            job=dict(c.execute('SELECT * FROM analysis_jobs WHERE id=?',(jid,)).fetchone())
            sources,metrics=load_catalog(c,job)
        if not metrics: raise ValueError('数据源尚未配置指标')
        if planning:
            analysis_event(jid,'正在根据指标目录生成分析方案')
            text,spec=plan(job,sources,metrics)
            active(jid)
            update(jid,status='awaiting_approval',plan=text,query_spec=json.dumps(spec,ensure_ascii=False),version=job['version']+1)
            analysis_event(jid,'分析方案已生成，等待负责人审批')
            return
        update(jid,status='analyzing');analysis_event(jid,'开始执行经过批准的指标计算')
        with connection() as c:
            sources,metrics,spec,snapshot=check_approval_snapshot(c,job)
        spec,rows,evidence=execute_with_evidence(sources,metrics,spec)
        evidence['approval_configuration_hash']=snapshot['configuration_hash']
        result={'report':'','query':spec,'rows':rows,'generated_at':time.time(),
                'source':'、'.join(source['name'] for source in sources),'row_count':len(rows),
                'evidence':evidence,'claims':[],'claim_validation':'model_pending'}
        active(jid);update(jid,status='reporting',result=json.dumps(result,ensure_ascii=False))
        analysis_event(jid,f'完成 {len(rows)} 个聚合结果，正在生成报告')
        summary,recommendations,claims,claim_validation=report(job,sources,metrics,spec,rows,evidence)
        active(jid)
        result.update(report=summary,generated_at=time.time(),claims=claims,claim_validation=claim_validation)
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
