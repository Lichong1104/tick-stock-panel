"""akshare 数据源 provider (新浪/腾讯免费源)。

方法签名对齐 custom.GenericHTTPProvider / fuyao provider, 注入 custom loader
注册表后, 各 service 无需改动即可路由到本 provider。

实现数据集:
  - realtime  A 股全市场快照 (ak.stock_zh_a_spot, 新浪, 1 次请求)
  - daily     A 股日K (ak.stock_zh_a_daily, 新浪, 不复权原始价, 逐标的)
  - minute    A 股分钟K (ak.stock_zh_a_minute, 新浪, 仅近期数据, 逐标的)
未声明 adj_factor/financial → provider_has_dataset 为 False, 自动回退 tickflow。

单位与口径 (CONTRIBUTING §3.1, 不可凭字段名推断):
  - sina 涨跌幅为百分数 (0.667 = +0.667%) → realtime change_pct 契约小数制 (/100)
  - sina volume 单位为股 → 手 (floor /100)
  - 日K/分钟取 adjust='' 不复权, 项目内前复权由 indicators.pipeline 用本地因子计算
  - 代码格式: 项目 600519.SH ↔ sina sh600519
"""
from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import polars as pl
import requests

from app.data_providers.normalizer import normalize_adj_factors, normalize_daily
from app.plugins.akshare.client import ak, from_sina, to_sina

logger = logging.getLogger(__name__)

# 只声明真实提供的数据集; 其余数据集 provider_has_dataset 返回 False → 回退 tickflow
_DATASETS = ("realtime", "daily", "minute", "adj_factor", "depth5", "financial")

# 项目 freq 契约 → 新浪 period 参数
_FREQ_MAP = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60", "1h": "60"}

# 逐标的请求节流 (新浪单标的接口, 避免触发限频)
_INTERVAL_S = 0.12

# 新浪财务三表字段 → 项目 canonical 列名 (TickFlow 口径, 前端财务页/回测 FUNDAMENTAL_FACTORS 消费)
_INCOME_FIELD_MAP = {
    "营业收入": "revenue",
    "营业成本": "operating_cost",
    "销售费用": "selling_expense",
    "管理费用": "admin_expense",
    "研发费用": "rd_expense",
    "财务费用": "financial_expense",
    "营业利润": "operating_profit",
    "利润总额": "total_profit",
    "所得税费用": "income_tax",
    "净利润": "net_income",
    "归属于母公司所有者的净利润": "net_income_attributable",
    "基本每股收益": "basic_eps",
}
_BALANCE_FIELD_MAP = {
    "资产总计": "total_assets",
    "流动资产合计": "total_current_assets",
    "非流动资产合计": "total_non_current_assets",
    "货币资金": "cash_and_equivalents",
    "应收账款": "accounts_receivable",
    "负债合计": "total_liabilities",
    "所有者权益(或股东权益)合计": "total_equity",
}
_CASHFLOW_FIELD_MAP = {
    "经营活动产生的现金流量净额": "net_operating_cash_flow",
    "投资活动产生的现金流量净额": "net_investing_cash_flow",
    "筹资活动产生的现金流量净额": "net_financing_cash_flow",
    "购建固定资产、无形资产和其他长期资产所支付的现金": "capex",
    "现金及现金等价物净增加额": "net_cash_change",
}
# 指标摘要(同花顺口径) → canonical metrics
_METRICS_FIELD_MAP = {
    "净资产收益率(ROE)": "roe",
    "总资产报酬率(ROA)": "roa",
    "毛利率": "gross_margin",
    "销售净利率": "net_margin",
    "资产负债率": "debt_to_asset_ratio",
    "营业总收入增长率": "revenue_yoy",
    "归属母公司净利润增长率": "net_income_yoy",
    "经营性现金净流量/营业总收入": "operating_cash_to_revenue",
    "基本每股收益": "eps_basic",
    "每股净资产": "bps",
}
# table 名 → (新浪报表符号, 字段映射)
_STATEMENTS = {
    "income": ("利润表", _INCOME_FIELD_MAP),
    "balance_sheet": ("资产负债表", _BALANCE_FIELD_MAP),
    "cash_flow": ("现金流量表", _CASHFLOW_FIELD_MAP),
}


def availability() -> tuple[bool, str]:
    """loader 启动自检: akshare 可导入即视为可用。不抛异常。"""
    try:
        import akshare  # noqa: F401

        return True, "ok"
    except Exception as e:  # noqa: BLE001
        return False, f"缺少 akshare 依赖(点击安装, 或 uv pip install akshare): {e}"


@dataclass
class _AkshareConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "akshare"
    display_name: str = "akshare"
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


def _int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


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


class AkshareProvider:
    """akshare 数据源。realtime = A 股全市场快照 (quote_service 全市场模式轮询调用)。"""

    name = "akshare"
    builtin = True

    def __init__(self) -> None:
        self.config = _AkshareConfig()

    def close(self) -> None:
        """akshare 无持久连接, 空实现。"""

    # ---- realtime ----
    def get_realtime(self) -> list[dict]:
        """全市场实时快照 → 内部 realtime records。失败软返回空列表(不阻断轮询)。"""
        try:
            spot = ak.stock_zh_a_spot()
        except Exception as e:  # noqa: BLE001
            logger.warning("akshare 实时行情拉取失败: %s", e)
            return []

        fetched_ms = int(time.time() * 1000)
        records: list[dict] = []
        for r in spot.to_dict("records"):
            code = r.get("代码")
            if not code:
                continue
            pct = _to_float(r.get("涨跌幅"))
            records.append(
                {
                    "symbol": from_sina(str(code)),
                    "name": r.get("名称"),
                    "last_price": _to_float(r.get("最新价")),
                    "prev_close": _to_float(r.get("昨收")),
                    "open": _to_float(r.get("今开")),
                    "high": _to_float(r.get("最高")),
                    "low": _to_float(r.get("最低")),
                    "volume": _hand(_to_float(r.get("成交量"))),
                    "amount": _to_float(r.get("成交额")),
                    # 百分数 (0.667 = +0.667%) → 小数制 (0.00667), 契约见模块 docstring
                    "change_pct": pct / 100.0 if pct is not None else None,
                    "change_amount": _to_float(r.get("涨跌额")),
                    "timestamp": fetched_ms,
                    "session": None,
                }
            )
        logger.info("akshare 实时行情拉取完成: %d 条", len(records))
        return records

    # ---- daily ----
    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """A 股日K → 内部契约。原始价(adjust='' 不复权)、volume 股→手(floor /100)。

        逐标的调用新浪 stock_zh_a_daily, 单标的失败软跳过不阻断整批。
        """
        if not symbols or asset_type != "stock":
            return pl.DataFrame()
        end_d = (end_time or datetime.now()).strftime("%Y%m%d")
        start_d = (start_time or datetime.now() - timedelta(days=365)).strftime("%Y%m%d")

        rows: list[dict] = []
        total = len(symbols)
        for i, sym in enumerate(symbols):
            if i:
                time.sleep(_INTERVAL_S)
            try:
                df = ak.stock_zh_a_daily(
                    symbol=to_sina(sym), start_date=start_d, end_date=end_d, adjust=""
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("akshare 日K拉取失败 %s: %s", sym, e)
                if on_chunk_done:
                    on_chunk_done(i + 1, total)
                continue
            for r in df.to_dict("records"):
                rows.append(
                    {
                        "symbol": sym,
                        "date": r.get("date"),
                        "open": _to_float(r.get("open")),
                        "high": _to_float(r.get("high")),
                        "low": _to_float(r.get("low")),
                        "close": _to_float(r.get("close")),
                        "volume": _hand(_to_float(r.get("volume"))),
                        "amount": _to_float(r.get("amount")),
                    }
                )
            if on_chunk_done:
                on_chunk_done(i + 1, total)
        return normalize_daily(rows, source=self.name)

    # ---- minute ----
    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """A 股分钟K → 内部契约 (symbol/datetime/OHLCV/amount)。

        新浪 stock_zh_a_minute 仅返回近期数据(无全量历史), 逐标的拉取。
        start_time/end_time 新浪分钟接口不按窗口过滤, 由下游裁剪。
        """
        if not symbols or asset_type != "stock":
            return pl.DataFrame()
        period = _FREQ_MAP.get(freq, "5")

        rows: list[dict] = []
        total = len(symbols)
        for i, sym in enumerate(symbols):
            if i:
                time.sleep(_INTERVAL_S)
            try:
                df = ak.stock_zh_a_minute(symbol=to_sina(sym), period=period, adjust="")
            except Exception as e:  # noqa: BLE001
                logger.warning("akshare 分钟K拉取失败 %s: %s", sym, e)
                if on_chunk_done:
                    on_chunk_done(i + 1, total)
                continue
            for r in df.to_dict("records"):
                rows.append(
                    {
                        "symbol": sym,
                        "datetime": r.get("day"),
                        "open": _to_float(r.get("open")),
                        "high": _to_float(r.get("high")),
                        "low": _to_float(r.get("low")),
                        "close": _to_float(r.get("close")),
                        "volume": _hand(_to_float(r.get("volume"))),
                        "amount": _to_float(r.get("amount")),
                    }
                )
            if on_chunk_done:
                on_chunk_done(i + 1, total)

        if not rows:
            return pl.DataFrame()
        out = pl.DataFrame(rows)
        if "datetime" in out.columns:
            if out.schema["datetime"] == pl.Utf8:
                # akshare 分钟K day 为字符串, 解析为北京时间墙钟 naive datetime
                out = out.with_columns(
                    pl.col("datetime")
                    .str.to_datetime(format="%Y-%m-%d %H:%M:%S", strict=False)
                    .cast(pl.Datetime("us"))
                )
            else:
                out = out.with_columns(pl.col("datetime").cast(pl.Datetime("us"), strict=False))
        for col in ("open", "high", "low", "close", "volume", "amount"):
            out = out.with_columns(pl.col(col).cast(pl.Float64, strict=False))
        keep = [c for c in ("symbol", "datetime", "open", "high", "low", "close", "volume", "amount") if c in out.columns]
        return out.select(keep)

    # ---- adj_factor ----
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """A 股除权因子 → 内部契约 (symbol/trade_date/ex_factor, 单事件比值非累积)。

        数据链: 巨潮资讯分红送配 (stock_dividend_cninfo) → 逐标的取不复权日K
        前收盘价 → 交易所公式 (P-D)/(1+S) 推导单事件因子。无配股数据。
        """
        schema = {"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64}
        if not symbols or asset_type != "stock":
            return pl.DataFrame(schema=schema)

        rows: list[dict] = []
        total = len(symbols)
        for i, sym in enumerate(symbols):
            if i:
                time.sleep(_INTERVAL_S)
            code = sym.split(".")[0]
            try:
                div = ak.stock_dividend_cninfo(symbol=code)
            except Exception as e:  # noqa: BLE001
                logger.warning("akshare 除权数据拉取失败 %s: %s", sym, e)
                if on_chunk_done:
                    on_chunk_done(i + 1, total)
                continue
            # 解析事件: (ex_date, 每股派息, 每股送转)
            events: list[tuple[date, float, float]] = []
            for r in div.to_dict("records"):
                ex_date = _parse_date(r.get("除权日"))
                if ex_date is None:
                    continue
                if start_time and ex_date < start_time.date():
                    continue
                if end_time and ex_date > end_time.date():
                    continue
                dividend = (_to_float(r.get("派息比例")) or 0.0) / 10.0
                bonus = (
                    (_to_float(r.get("送股比例")) or 0.0) + (_to_float(r.get("转增比例")) or 0.0)
                ) / 10.0
                events.append((ex_date, dividend, bonus))
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
                    continue  # 日K窗口未覆盖的远古事件
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
        """不复权日K → {date: close}, 供除权因子配价。失败软返回空。"""
        try:
            df = ak.stock_zh_a_daily(
                symbol=to_sina(symbol),
                start_date=start_d.strftime("%Y%m%d"),
                end_date=end_d.strftime("%Y%m%d"),
                adjust="",
            )
        except Exception:  # noqa: BLE001
            return {}
        out: dict[date, float] = {}
        for r in df.to_dict("records"):
            d = r.get("date")
            c = _to_float(r.get("close"))
            if d is not None and c is not None:
                out[d] = c
        return out

    # ---- depth5 ----
    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict]:
        """A 股五档盘口 → {symbol: {bid_volumes, ask_volumes}} (腾讯 qt.gtimg.cn)。

        量单位: 手 (与项目契约一致)。逐标的请求, 失败软跳过。
        """
        result: dict[str, dict] = {}
        for sym in symbols:
            try:
                r = requests.get(f"https://qt.gtimg.cn/q={to_sina(sym)}", timeout=5)
                txt = r.content.decode("gbk", errors="ignore")
                if '="' in txt:
                    txt = txt.split('="', 1)[1].rstrip('";')
                f = txt.split("~")
                if len(f) < 29:
                    continue
                result[sym] = {
                    "bid_volumes": [_int(f[i]) for i in (10, 12, 14, 16, 18)],
                    "ask_volumes": [_int(f[i]) for i in (20, 22, 24, 26, 28)],
                }
            except Exception:  # noqa: BLE001
                continue
        return result

    # ---- financial ----
    def get_financials(
        self,
        table: str,
        symbols: list[str],
        latest_only: bool = True,
    ) -> pl.DataFrame:
        """A 股财务 → 内部 canonical 列 (symbol/period_end/announce_date/指标)。

        - 三大报表: 新浪 stock_financial_report_sina, 字段映射为 TickFlow 口径,
          latest_only 决定最近 1 期还是 8 期;
        - metrics: 新浪 stock_financial_abstract 指标摘要, 恒只取最近一期;
        - shares: 新浪无股本接口, 恒返回空 (已有存量靠合并写入保留)。
        """
        if table == "shares":
            logger.info("akshare 无股本接口, shares 表跳过")
            return pl.DataFrame()
        if table in _STATEMENTS:
            stmt_symbol, field_map = _STATEMENTS[table]
            return self._financial_statement(stmt_symbol, field_map, symbols, latest_only)
        if table == "metrics":
            return self._financial_metrics(symbols)
        return pl.DataFrame()

    def _financial_statement(
        self,
        stmt_symbol: str,
        field_map: dict[str, str],
        symbols: list[str],
        latest_only: bool,
    ) -> pl.DataFrame:
        rows: list[dict] = []
        for i, sym in enumerate(symbols):
            if i:
                time.sleep(_INTERVAL_S)
            try:
                df = ak.stock_financial_report_sina(stock=to_sina(sym), symbol=stmt_symbol)
            except Exception as e:  # noqa: BLE001
                logger.warning("akshare 财务 %s %s 失败: %s", stmt_symbol, sym, e)
                continue
            if df is None or df.empty or "报告日" not in df.columns:
                continue
            if "类型" in df.columns:
                df = df[df["类型"] == "合并期末"]
            df = df.sort_values("报告日", ascending=False)
            df = df.head(1 if latest_only else 8)
            for r in df.to_dict("records"):
                period_end = _parse_date(r.get("报告日"))
                if period_end is None:
                    continue
                announce = _parse_date(r.get("公告日期"))
                row: dict = {
                    "symbol": sym,
                    "period_end": period_end.isoformat(),
                    "announce_date": announce.isoformat() if announce else period_end.isoformat(),
                }
                for src, dst in field_map.items():
                    row[dst] = _to_float(r.get(src))
                rows.append(row)
        return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()

    def _financial_metrics(self, symbols: list[str]) -> pl.DataFrame:
        rows: list[dict] = []
        for i, sym in enumerate(symbols):
            if i:
                time.sleep(_INTERVAL_S)
            code = sym.split(".")[0]
            try:
                df = ak.stock_financial_abstract(symbol=code)
            except Exception as e:  # noqa: BLE001
                logger.warning("akshare 指标 %s 失败: %s", sym, e)
                continue
            if df is None or df.empty or "指标" not in df.columns:
                continue
            period_cols = [c for c in df.columns if c not in ("选项", "指标")]
            if not period_cols:
                continue
            latest_col = period_cols[0]  # 抽象表列按报告期降序, 首列即最新一期
            period_end = _parse_date(latest_col)
            if period_end is None:
                continue
            row: dict = {"symbol": sym, "period_end": period_end.isoformat()}
            for src, dst in _METRICS_FIELD_MAP.items():
                match = df[df["指标"] == src]
                if not match.empty:
                    row[dst] = _to_float(match.iloc[0][latest_col])
            rows.append(row)
        return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()

    def trading_days(self) -> set:
        """A 股交易日历 (供交易日探针)。失败抛异常由探针兜为未知。"""
        df = ak.tool_trade_date_hist_sina()
        col = "trade_date" if "trade_date" in df.columns else df.columns[0]
        return {d for d in df[col].tolist()}

    # ---- 测试(设置页试拉) ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        syms = [s for s in (symbols or [])][:3] or ["000001.SZ"]
        now = datetime.now()
        try:
            if dataset == "realtime":
                rows = self.get_realtime()
                return {
                    "provider": self.name,
                    "dataset": dataset,
                    "rows": len(rows),
                    "columns": list(rows[0].keys()) if rows else [],
                    "preview": rows[:5],
                }
            if dataset == "daily":
                df = self.get_daily(syms, now - timedelta(days=30), now)
            elif dataset == "minute":
                df = self.get_minute(syms, None, None, "stock", "1m")
            elif dataset == "adj_factor":
                df = self.get_adj_factors(syms, now - timedelta(days=365 * 3), now)
            elif dataset == "depth5":
                data = self.get_depth_batch(syms)
                return {
                    "provider": self.name,
                    "dataset": dataset,
                    "rows": len(data),
                    "columns": ["symbol", "bid_volumes", "ask_volumes"],
                    "preview": [{"symbol": s, **v} for s, v in list(data.items())[:5]],
                }
            elif dataset == "financial":
                df = self.get_financials("metrics", syms, latest_only=True)
            else:
                return {
                    "provider": self.name,
                    "dataset": dataset,
                    "rows": 0,
                    "error": f"akshare 插件未接入 {dataset} 数据集(自动回退 TickFlow)",
                }
        except Exception as e:  # noqa: BLE001
            return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
        head = df.head(5).to_dicts()
        for row in head:  # date/datetime → ISO 字符串, 保证 JSON 可序列化
            for k, v in list(row.items()):
                if isinstance(v, (datetime,)):
                    row[k] = v.isoformat()
                elif hasattr(v, "isoformat"):
                    row[k] = v.isoformat()
        return {
            "provider": self.name,
            "dataset": dataset,
            "rows": df.height,
            "columns": df.columns,
            "preview": head,
        }
