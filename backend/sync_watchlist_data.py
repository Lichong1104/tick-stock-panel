# -*- coding: utf-8 -*-
"""精准同步自选股的财务 + 除权因子 (逐标的 akshare, 全市场太慢)。"""
import polars as pl

from app.config import settings
from app.services import financial_sync as fs
from app.services import preferences
from app.data_providers import custom as custom_sources

SYMBOLS = [
    "600519.SH", "600036.SH", "601318.SH", "600030.SH", "000001.SZ",
    "000858.SZ", "000333.SZ", "300750.SZ", "002594.SZ", "601899.SH",
    "600900.SH", "600276.SH", "000651.SZ", "600887.SH", "601166.SH",
    "600487.SH", "600488.SH", "600489.SH", "601012.SH", "600809.SH",
]

data_dir = settings.data_dir

# 1) 财务三表 + 指标
for table in ["income", "balance_sheet", "cash_flow", "metrics"]:
    n = fs._sync_table(table, SYMBOLS, data_dir, None, latest_only=False)
    print(f"financial {table}: {n} 行")

# 2) 除权因子
provider = custom_sources.get_provider(preferences.get_adj_factor_provider())
new_data = provider.get_adj_factors(SYMBOLS, None, None, "stock")
out = data_dir / "adj_factor" / "all.parquet"
out.parent.mkdir(parents=True, exist_ok=True)
if out.exists():
    existing = pl.read_parquet(out)
    merged = (
        pl.concat([existing, new_data])
        .unique(subset=["symbol", "trade_date"], keep="last")
        .sort(["symbol", "trade_date"])
    )
    merged.write_parquet(out)
    print(f"adj_factor: 合并后 {merged.height} 行")
else:
    new_data.sort(["symbol", "trade_date"]).write_parquet(out)
    print(f"adj_factor: {new_data.height} 行")

print("done")
