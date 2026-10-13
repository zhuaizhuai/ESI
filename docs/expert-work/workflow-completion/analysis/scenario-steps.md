版本：V1.0；日期：2026-10-13 Asia/Shanghai。

可执行请求场景在 backend/tests/test_analysisflow.py 的 TestClient 测试：创建数据源和指标 → 创建任务 → 待审批编辑brief → 校验版本409 → 批准 → worker执行 → GET证据 → 授权/撤权核对。
纯计算场景同文件用临时CSV/SQLite及httpx MockTransport；命令见05-selftest。为避免保存登录凭据，不复制Cookie到静态请求文件。
