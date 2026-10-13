版本：V1.1；日期：2026-10-13 Asia/Shanghai。

BE-1 analytics.py：R1修复与R5受控计算、流式质量计数、输入指纹、聚合和计算证据。
BE-2 analysis.py：brief、配置冻结/漂移校验、幂等列迁移、权限证据API。
BE-3 analysis_worker.py：目录统一加载、执行审批复核、冻结配置执行、模型证据引用验证及旧summary兼容。
BE-4 test_analysisflow.py：真实临时CSV/SQLite、多源、HTTPmock、配置漂移、brief版本/权限、报告引用错误。
集成main/auth/orchestration/http_connector由主智能体负责；recommendations保留原字段契约。

BE-5：集成审查ISSUE-4，active授权失败的UPDATE只有影响任务行时才同步failed流程。并发取消或中断已经提交时，保留主任务和步骤终态一致。回归测试独立连接在权限hook内提交真实状态变更，分别覆盖cancelled/interrupted。
