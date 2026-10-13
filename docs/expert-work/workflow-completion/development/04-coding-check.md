# 编码核对
版本：V1.3
C1：development.py router/Depends/current_user/HTTPException符合platform样板。C2：save_plan BEGIN IMMEDIATE、参数化SQL和版本条件；checkpoint限定列白名单。C3：require_task_projects/require_system，对隐藏节点停止遍历。C4：init_development加法schema0保留历史；模块幂等初始化。C5：实际Git工作区、真实子进程和API测试；模型scripted，外部模型待验证。C6：仅专属代码/测试/文档，worker共享main改动已协调。
Python compileall与git diff --check通过。Java专属项不涉及。无未关闭规范违规。

追加：acceptance.py按当前账户与资源负责人权限验证，事务内版本状态检查，全资源指纹/仍授权复核；GET不泄露未授权结果，原diff截断也保存全文指纹。主键索引1个，无外部数据库执行。

最终核对：全文文件hash流式读取，固定基点，暂存前后同值；符号链接记录目标字符串不跟随。schema1验收必须匹配最终验证hash。compileall和git diff --check再次通过。
