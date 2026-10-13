# 编码规则
版本：V1.0
C1 路由APIRouter、当前用户Depends(current_user)、HTTPException。C2 数据通过core.connection参数化SQL，变更BEGIN IMMEDIATE并乐观版本检查。C3 权限复用require_task_projects/require_system；不暴露隐藏依赖路径。C4 扩展schema加法迁移、保留旧任务。C5 pytest/TestClient及临时git仓验证真实diff；真实模型/企业API不在本轮可用范围。C6 不修改共享模块或自动外部发布。Java规则不涉及：Python仓。
