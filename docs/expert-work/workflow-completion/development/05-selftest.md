# 研发自测
版本：V1.3；2026-10-13 Asia/Shanghai；基点953afb994b372ca4b64434f0569d2f5211b1d05f加本次未提交研发代码。
命令：cd backend && .venv/bin/python -m pytest tests/test_development.py -q。结果：24 passed in 13.90s，退出0。compileall -q app、git diff --check退出0。
覆盖：可行性/缺信息门禁；相对路径与敏感路径11例；全部仓库覆盖/循环依赖；版本冲突、废弃批准；viewer/outsider读写拒绝；可见传递依赖及隐藏节点隔离；真实Git工作区成功、验证生成越界文件失败保留；双仓依赖未验证写入拒绝和上游改动废弃下游；检查命令快照漂移；有效模型规划与仅summary不猜范围；显式独立验证脚本保护。
接口通过FastAPI TestClient真实ASGI路由/SQLite状态断言；模型由scripted替身，不代表真实模型质量。子进程本地执行，不代表真实Docker/CI/外部发布验证。首次测试1例因共享projects增加列导致INSERT VALUES不匹配，改显式列后全部通过。
基线tests/test_execution.py：改代码前8 passed in6.55s。最终全量交由主智能体集成执行。

追加验收批次：.venv/bin/python -m pytest tests/test_acceptance.py tests/test_development.py::test_authorization_failure_cannot_overwrite_cancelled_workflow -q -> 13 passed in13.72s（12验收+1竞态）。真实双Git仓检查差异隔离、截断尾部/未暂存文件内容变化废弃旧验收。首次fixed task_id遇未清task_projects旧关联碰撞，改独立uuid后通过，已通知root清关联表。

最终批次：.venv/bin/python -m pytest tests/test_development.py tests/test_acceptance.py -q -> 39 passed in29.63s，退出0。随后新增缺少verification_hashes明确门禁测试单独运行 ->1 passed in0.71s。当前28研发+12验收各用例全部验证；最终全量由root运行。compileall和diff规范检查退出0。
