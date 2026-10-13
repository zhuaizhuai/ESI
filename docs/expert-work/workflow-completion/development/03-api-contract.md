# 研发契约
版本：V1.3，设计已消息交接，实际实现以backend/app/development.py为准。
GET /api/development/tasks/{tid}/plan -> {version,plan,projects,approvable,validation_errors}。POST同路径 {version,plan} -> {ok,version}。GET /api/development/impacts/{project_id} -> 项目影响数组（project_id/name,system_id/name,distance,chain,relationship）。GET /api/development/tasks/{tid}/delivery -> {task_id,status,version,projects[]}。
全仓viewer读；创建人且全仓developer或全仓maintainer写；资源不存在/无权404，编辑越权403，参数400，状态/版本冲突409。
计划字段：objective,feasibility(feasible/needs_information/infeasible),questions[],risks[],projects[{project_id,impact,allowed_paths[],validation[],rollback,depends_on[],review_tests:false}]。
主程序注册router/init_development，新任务plan_schema=1；审批require_approvable(c,task)；旧自由文本更新清空structured_plan并invalidate_approvals。

补充：GET plan提供warnings，与validation_errors一致；GET delivery提供approved_checks。项目验证配置快照变动时批准/执行409；新任务缺快照需重新保存。run_check {project_id}检查单仓；run_check {}只检查已经开始且依赖就绪的仓；修改上游废弃传递下游验证，最终全仓验证。

验收源码backend/app/acceptance.py：GET /api/acceptance/inbox -> {items:[{kind,job_id,title,version,resource_id,name,scoped_result,can_accept:true}]}；POST /api/acceptance/{kind}/{job_id} {version,resource_ids:[]} -> {ok,completed,version,resource_ids}。研发scoped_result {checks,changed_files,base,diff,diff_truncated,content_hash}；分析 {rows,metrics,quality,source,limitations,claim_validation}。不返回其他仓差异或整报告。空resource_ids选择自己负责资源；无读取404/read-only403/版本与未完成结果409。resource_acceptances自动幂等init；accept_resources require_all=True供旧complete包装。

最终契约：研发result新增verification_hashes{project_id:hash}。schema1没有此字段或实时工作区hash不一致时验收409“请重新执行验证”；该检查发生在首次及重复验收，仍不返回其他资源内容。
