"""baostock 客户端封装。

- baostock 走自有 TCP 协议 (非 HTTP), 无需代理处理。
- 代码格式: 项目 600519.SH ↔ baostock sh.600519。
"""
from __future__ import annotations

import baostock as bs


def to_bs(symbol: str) -> str:
    """项目代码格式 → baostock: '600519.SH' -> 'sh.600519'。"""
    code, _, market = symbol.partition(".")
    return f"{market.lower()}.{code}"
