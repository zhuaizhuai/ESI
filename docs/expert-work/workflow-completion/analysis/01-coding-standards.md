版本：V1.0；日期：2026-10-13 Asia/Shanghai。

- Python snake_case；按现有模块以纯函数计算、APIRouter 暴露服务。
- 输入用 Pydantic 和服务端参数校验，客户端身份不得用作权限事实。
- SQLite 参数 SQL，标识符白名单列目录 + quote；加法迁移幂等。
- 写事务不访问 HTTP；来源秘密不进入日志和报告。
- 模型只收到聚合和定义，不收到原始明细；不允许任意 SQL。
- 测试真实临时 CSV/SQLite、httpx MockTransport 和 TestClient；真实模型/外部系统未配置。
- 本仓非 Java，不适用 Java 命名与 Spring 规范。
