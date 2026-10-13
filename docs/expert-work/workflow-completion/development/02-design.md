# 研发设计
版本：V1.3
R3：方案校验覆盖所有项目、路径相对且受保护目录拒绝、依赖DAG、可行性/问题门禁；执行前重新验证，写入与最终git范围双检查。既有测试修改需review_tests且配置验证文件不可改。每仓阶段/基准/结果持久化，不将检查点声明生产回滚。R4：乐观方案版本，变更废弃审批，执行require_execution_approvals。R8：tasks structured_plan TEXT / plan_schema INTEGER DEFAULT0保留旧任务，development_runs主键(task_id,project_id)保存阶段/checks/changed_files/error。
影响调研：双向可见系统关系BFS，输出距离和可见链，拒绝穿越无权系统；目录不等价自动代码依赖发现。
交付包：每仓基准/文件变化/检查/审批版本/手动PR及CI交接；失败也保留。没有发布集成，不自动推送。

补充：tasks.validation_snapshot冻结各项目管理员配置的检查命令，漂移必须新版本审批；既有验证/CI配置和检查命令显式引用的验证文件不可修改，即便review_tests模式。上游检查通过才允许下游写入，上游内容变更废弃自身和传递下游验证；重试重新验证，不复用内存授权。

追加跨系统验收：resource_acceptances四字段主键、仅主键索引；每资源独立负责人签收。GET inbox只自己资源，分析不展示整报告/跨源计算。验收绑定版本+整体结果指纹+本仓完整diff/文件内容指纹；全部签收且人员当前权限有效才completed。旧complete包装all权限，新接口不要求其他资源读权限。取消授权失败竞态仅rowcount改变后同步workflow。

最终修补：working_content_hash以固定base为基准，覆盖变更内容/未跟踪/删除，与暂存状态无关；Worker最终验证和diff后保存verification_hashes。新任务缺指纹或验收时当前代码漂移409要求重验证，防首次验收接受未验证新代码。历史schema0无指纹保持兼容。
