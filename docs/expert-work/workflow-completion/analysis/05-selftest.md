版本：V1.1；日期：2026-10-13 Asia/Shanghai。

基点953afb994b372ca4b64434f0569d2f5211b1d05f，未提交工作树。Python3.9虚拟环境，真实临时SQLite/CSV及FastAPI TestClient（同本仓SQL方言）。

`PYTHONPATH=backend backend/.venv/bin/python -m pytest backend/tests/test_analysisflow.py backend/tests/test_analytics.py -q`：21通过，退出0。

R1：201分组CSV/SQLite显式失败；HTTP 0、false、null、空串四组；结束日期23:59:59.999/空格日期包含；无效日期与数字质量计数一致。
R5：跨源不同维度名称映射、缺组、零分母、同单位、固定比例情景；HTTPMock无明细泄露；批准冻结实际选中指标；term重绑与connector_config变化拒绝；实时数据变化允许；brief版本冲突；证据匿名401、无授权404、授权viewer210、撤权404；claim未知引用失败，计算证据仍可查询。

旧test_analytics4项继续通过，旧summary兼容并标人工审核。另一test_platform权限返回差异（期望403实际404）归主智能体actions处理，本模块不修改其他所有权文件。

真实模型、真实企业HTTP与效果归因未配置，保持待验证。不是模型引用存在即可判正文和因果正确。

追加接口验证：同单位术语绑定后的差值计划可通过现有plan API保存和批准；main需使用load_catalog读取term_unit，已由根智能体集成。

V1.1回归批次：test_concurrent_terminal_state_is_not_overwritten_by_authorization_failure，分别cancelled/interrupted；另一连接在权限hook内更新任务并sync_workflow、提交，再抛403。断言主任务状态及所有步骤与并发操作提交结果逐项相同。

V1.1同一聚焦命令：23通过，退出0，9.49秒。确认此次代码变化后的结果，无沿用旧批次证据。
