版本：V1.0；日期：2026-10-13 Asia/Shanghai。

仓库 /Users/zhuai.wang/zhuai/ESI；固定基点 953afb994b372ca4b64434f0569d2f5211b1d05f。
FastAPI + SQLite；样板 backend/app/platform.py 路由权限，backend/app/analytics.py 计算。
真实基线：`PYTHONPATH=backend backend/.venv/bin/python -m pytest backend/tests/test_analytics.py -q`，4 通过。
系统 Python 未设置 PYTHONPATH 首次失败；使用项目虚拟环境和明确路径后成功。
