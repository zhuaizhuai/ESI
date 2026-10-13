版本：V1.0；日期：2026-10-13 Asia/Shanghai；实现依据 backend/app/analysis.py，字段以类型及路由源码为准。

GET `/api/analysis/jobs/{jid}/brief` 返回 `{version,brief:{decision,objective,constraints,assumptions}}`。
POST 同路径接收 `{version,brief}`；要求所有源 analyst 且自己创建，或所有源 owner；待审批、版本一致才修改；版本增加并清审批/快照。

GET `/api/analysis/jobs/{jid}/evidence` 需全部数据源 viewer；返回 sources、metrics、quality、calculations、definition_hash、complete、limitations、rows、claims、claim_validation、approval_snapshot。无结果409；无身份401；无资源权限404；版本/状态冲突409。旧报告 complete=false并说明未保存证据。

`freeze_approval(c,job)` 和 `check_approval_snapshot(c,job)` 由批准/执行调用，配置变化抛ValueError，API转409。源配置hash包括 id/kind/path/auth_env/connector_config，不包括令牌值。
`execute_with_evidence(sources,metrics,spec)` 返回 normalized,rows,evidence；不改变数据源。每行 evidence_id，各计算附输入 evidence_ids。旧execute_multi保留两元返回。

Query可选 comparisons/scenarios及item.dimension_aliases，约束见validate_spec。前端与根智能体已同步契约。真实业务连接与模型待企业配置。
