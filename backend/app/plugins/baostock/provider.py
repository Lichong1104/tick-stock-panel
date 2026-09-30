"""baostock 数据源 provider (免费开源 A 股数据, 无需注册/Key)。

方法签名对齐 custom.GenericHTTPProvider / akshare provider, 注入 custom loader
注册表后, 各 service 无需改动即可路由到本 provider。

实现数据集:
  - daily       A 股日K (query_history_k_data_plus, 不复权原始价, 逐标的)
  - adj_factor  A 股除权因子 (query_dividend_data 官方分红送配 + 日K 前收盘价推导)
未声明 realtime/minute/depth5/financial → 自动回退 (akshare 或 tickflow)。

单位与口径:
  - baostock volume 单位为股 → 手 (floor /100)
  - 分红送配每股口径 (dividCashPsBeforeTax / dividStocksPs / dividReserveToStockPs)
    已是每 1 股值, 直接套交易所公式 (P-D)/(1+S)
  - 日K取 adjustflag='3' 不复权, 项目内前复权由 indicators.pipeline 用本地因子计算
"""
from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import polars as pl

import baostock as bs

from app.data_providers.normalizer import normalize_adj_factors, normalize_daily
from app.plugins.baostock.client import to_bs

logger = logging.getLogger(__name__)

_DATASETS = ("daily", "adj_factor")
_INTERVAL_S = 0.2  # baostock 单标的节流


def availability() -> tuple[bool, str]:
    """loader 启动自检: baostock 可导入即视为可用。不抛异常。"""
    try:
        import baostock  # noqa: F401

        return True, "ok"
    except Exception as e:  # noqa: BLE001
        return False, f"缺少 baostock 依赖(点击安装, 或 uv pip install baostock): {e}"


@dataclass
class _BaostockConfig:
    name: str = "baostock"
    display_name: str = "baostock"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return f


def _hand(value) -> float | None:
    """股 → 手 (floor)。"""
    return math.floor(value / 100.0) if value is not None else None


def _parse_date(value) -> date | None:
    if value is None:
        return None
    try:
        if value != value:  # pandas NaT / NaN (自比较为 True)
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, datetime):
        d = value.date()
        try:
            if d != d:  # NaT.date() 也可能是 NaT
                return None
        except (TypeError, ValueError):
            pass
        return d
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _ref_price(prev_close: float, dividend: float, bonus: float) -> float | None:
    """交易所除权参考价 (无配股): (P - D) / (1 + S)。"""
    denom = 1.0 + bonus
    if denom <= 0:
        return None
    x = (prev_close - dividend) / denom
    return math.floor(x * 100 + 0.5) / 100


class BaostockProvider:
    """baostock 数据源。daily = 历史日K, adj_factor = 除权因子。"""

    name = "baostock"
    builtin = True

    def __init__(self) -> None:
        self.config = _BaostockConfig()
        self._logged_in = False

    def _login(self) -> bool:
        if not self._logged_in:
            try:
                lg = bs.login()
                self._logged_in = lg.error_code == "0"
            except Exception as e:  # noqa: BLE001
                logger.warning("baostock 登录失败: %s", e)
                self._logged_in = False
        return self._logged_in

    def close(self) -> None:
        if self._logged_in:
            with __import__("contextlib").suppress(Exception):
                bs.logout()
            self._logged_in = False

    # ---- daily ----
    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        if not symbols or asset_type != "stock" or not self._login():
            return pl.DataFrame()
        start_d = (start_time or datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        end_d = (end_time or datetime.now()).strftime("%Y-%m-%d")

        rows: list[dict] = []
        total = len(symbols)
        for i, sym in enumerate(symbols):
            if i:
                time.sleep(_INTERVAL_S)
            try:
                rs = bs.query_history_k_data_plus(
                    to_bs(sym), "date,open,high,low,close,volume,amount",
                    start_date=start_d, end_date=end_d, frequency="d", adjustflag="3",
                )
                while rs.next():
                    rec = rs.get_row_data()
                    if len(rec) < 7:
                        continue
                    rows.append(
                        {
                            "symbol": sym,
                            "date": _parse_date(rec[0]),
                            "open": _to_float(rec[1]),
                            "high": _to_float(rec[2]),
                            "low": _to_float(rec[3]),
                            "close": _to_float(rec[4]),
                            "volume": _hand(_to_float(rec[5])),
                            "amount": _to_float(rec[6]),
                        }
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning("baostock 日K拉取失败 %s: %s", sym, e)
            if on_chunk_done:
                on_chunk_done(i + 1, total)
        return normalize_daily(rows, source=self.name)

    # ---- adj_factor ----
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        schema = {"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64}
        if not symbols or asset_type != "stock" or not self._login():
            return pl.DataFrame(schema=schema)
        start_year = (start_time or datetime.now() - timedelta(days=365 * 20)).year
        end_year = (end_time or datetime.now()).year

        rows: list[dict] = []
        total = len(symbols)
        for i, sym in enumerate(symbols):
            if i:
                time.sleep(_INTERVAL_S)
            # 按年查除权除息事件 (baostock 接口按年份)
            events: list[tuple[date, float, float]] = []
            for year in range(start_year, end_year + 1):
                try:
                    rs = bs.query_dividend_data(to_bs(sym), year, "report")
                    while rs.next():
                        rec = dict(zip(rs.fields, rs.get_row_data()))
                        ex_date = _parse_date(rec.get("dividOperateDate"))
                        if ex_date is None:
                            continue
                        if start_time and ex_date < start_time.date():
                            continue
                        if end_time and ex_date > end_time.date():
                            continue
                        dividend = _to_float(rec.get("dividCashPsBeforeTax")) or 0.0
                        bonus = (
                            (_to_float(rec.get("dividStocksPs")) or 0.0)
                            + (_to_float(rec.get("dividReserveToStockPs")) or 0.0)
                        )
                        events.append((ex_date, dividend, bonus))
                except Exception as e:  # noqa: BLE001
                    logger.warning("baostock 除权数据 %s %s 失败: %s", sym, year, e)
            if not events:
                if on_chunk_done:
                    on_chunk_done(i + 1, total)
                continue
            closes = self._daily_closes(
                sym, min(e[0] for e in events) - timedelta(days=30), max(e[0] for e in events)
            )
            days = sorted(closes)
            for ex_date, dividend, bonus in events:
                prev_days = [d for d in days if d < ex_date]
                if not prev_days:
                    continue
                prev_close = closes[prev_days[-1]]
                ref = _ref_price(prev_close, dividend, bonus)
                if ref is None or ref <= 0:
                    continue
                rows.append({"symbol": sym, "trade_date": ex_date, "ex_factor": prev_close / ref})
            if on_chunk_done:
                on_chunk_done(i + 1, total)

        if not rows:
            return pl.DataFrame(schema=schema)
        return normalize_adj_factors(rows, source=self.name)

    def _daily_closes(self, symbol: str, start_d: date, end_d: date) -> dict[date, float]:
        rs = bs.query_history_k_data_plus(
            to_bs(symbol), "date,close",
            start_date=start_d.strftime("%Y-%m-%d"), end_date=end_d.strftime("%Y-%m-%d"),
            frequency="d", adjustflag="3",
        )
        out: dict[date, float] = {}
        while rs.next():
            rec = rs.get_row_data()
            d = _parse_date(rec[0])
            c = _to_float(rec[1])
            if d is not None and c is not None:
                out[d] = c
        return out

    # ---- 测试(设置页试拉) ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        syms = [s for s in (symbols or [])][:3] or ["000001.SZ"]
        now = datetime.now()
        try:
            if dataset == "daily":
                df = self.get_daily(syms, now - timedelta(days=30), now)
            elif dataset == "adj_factor":
                df = self.get_adj_factors(syms, now - timedelta(days=365 * 3), now)
            else:
                return {
                    "provider": self.name,
                    "dataset": dataset,
                    "rows": 0,
                    "error": f"baostock 插件未接入 {dataset} 数据集",
                }
        except Exception as e:  # noqa: BLE001
            return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
        head = df.head(5).to_dicts()
        for row in head:
            for k, v in list(row.items()):
                if hasattr(v, "isoformat"):
                    row[k] = v.isoformat()
        return {
            "provider": self.name,
            "dataset": dataset,
            "rows": df.height,
            "columns": df.columns,
            "preview": head,
        }
