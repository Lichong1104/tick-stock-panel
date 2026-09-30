"""akshare 客户端封装。

关键点:
  - 本网络下东财(push2his.eastmoney.com)不可达, 统一走新浪/腾讯接口。
  - 新浪/腾讯是国内站, 需直连绕过系统代理(Clash)。requests 在 Windows 上
    会读注册表系统代理导致连接被重置, 这里在导入 akshare 前屏蔽代理探测。
  - 代码格式转换: 项目 600519.SH ↔ akshare 新浪 sh600519。
"""
from __future__ import annotations

import os

# 直连绕过系统代理: 清空代理环境变量 + 屏蔽 getproxies(Windows 注册表代理)。
# tickflow.org 直连可达(已实测), 全局屏蔽不影响 TickFlow SDK。
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")
os.environ.setdefault("TQDM_DISABLE", "1")  # 屏蔽 akshare 分页进度条(后端日志不刷屏)
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import urllib.request  # noqa: E402

urllib.request.getproxies = lambda: {}  # 屏蔽 Windows 注册表系统代理

import akshare as ak  # noqa: E402


def to_sina(symbol: str) -> str:
    """项目代码格式 → 新浪代码: '600519.SH' -> 'sh600519'。"""
    code, _, market = symbol.partition(".")
    return (market or "").lower() + code


def from_sina(code: str) -> str:
    """新浪代码 → 项目代码格式: 'sh600519' -> '600519.SH'。"""
    if len(code) < 3:
        return code
    return code[2:] + "." + code[:2].upper()
