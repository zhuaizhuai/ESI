版本：V1.1；日期：2026-10-13 Asia/Shanghai。

实际规范依据01-coding-standards.md。analytics.py允许字段quote且读取只读游标；analysis.py用户来自current_user并复核各源；事务内无HTTP；analysis_worker仅聚合输入模型。所有SQL参数值绑定，迁移只增列。来源秘密未输出，原始明细只用来计算hash/聚合。新模块对照platform.py route样板与analytics.py纯函数风格。Java规则不适用。

契约字段与03-api-contract一致。超限必须失败，无截断成功路径；时间范围统一ISO日期；比较同公共维度同粒度；引用仅验证ID存在，人工核对正文保留。
基线4通过，新测试首轮11通过/3失败，2项夹具修复、router集成后通过。当前未关闭代码违规0。
最终聚焦命令 `PYTHONPATH=backend backend/.venv/bin/python -m pytest backend/tests/test_analysisflow.py backend/tests/test_analytics.py -q`：21项通过。

V1.1 ISSUE-4修复：analysis_worker.active按changed.rowcount维护流程，状态保护条件与流程同步一致；独立SQLite连接构造并发终态提交，不以单连接修改模拟事务效果。最终23项通过，退出0，9.49秒。
