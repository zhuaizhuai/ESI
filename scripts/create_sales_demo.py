#!/usr/bin/env python3
"""Create a deterministic, read-only-friendly sales database for the ESI demo."""

import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "runtime" / "sales-demo.db"

ROWS = [
    ("2026-01-03", "华东", "线上", "智能终端", "C001", 2, 15800.0, "paid"),
    ("2026-01-09", "华南", "渠道", "协作套件", "C002", 8, 24000.0, "paid"),
    ("2026-01-18", "华北", "直销", "数据服务", "C003", 1, 32000.0, "paid"),
    ("2026-01-26", "华东", "直销", "数据服务", "C004", 1, 28000.0, "paid"),
    ("2026-02-04", "华南", "线上", "智能终端", "C005", 3, 22500.0, "paid"),
    ("2026-02-11", "华东", "渠道", "协作套件", "C002", 10, 30000.0, "paid"),
    ("2026-02-19", "华北", "线上", "智能终端", "C006", 1, 7900.0, "refunded"),
    ("2026-02-25", "华东", "直销", "数据服务", "C007", 2, 61000.0, "paid"),
    ("2026-03-02", "华南", "直销", "数据服务", "C008", 1, 35000.0, "paid"),
    ("2026-03-08", "华北", "渠道", "协作套件", "C009", 12, 36000.0, "paid"),
    ("2026-03-16", "华东", "线上", "智能终端", "C001", 4, 31600.0, "paid"),
    ("2026-03-24", "华南", "线上", "协作套件", "C010", 6, 18000.0, "paid"),
]


def main():
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    if TARGET.exists():
        raise SystemExit(f"目标文件已存在，未覆盖：{TARGET}")
    connection = sqlite3.connect(TARGET)
    connection.execute(
        """CREATE TABLE orders(
        order_date TEXT NOT NULL,
        region TEXT NOT NULL,
        channel TEXT NOT NULL,
        product TEXT NOT NULL,
        customer_id TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        amount REAL NOT NULL,
        status TEXT NOT NULL
        )"""
    )
    connection.executemany("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?)", ROWS)
    connection.commit()
    connection.close()
    print(TARGET)
    print("建议指标：销售额=sum(amount)，订单数=count，客户数=count_distinct(customer_id)")
    print("日期列：order_date；可用维度：region, channel, product, status")


if __name__ == "__main__":
    main()
