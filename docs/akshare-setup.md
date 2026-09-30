# akshare 免费数据源接入说明

本项目已内置 **akshare** 与 **baostock** 两个免费数据源插件，可替代 TickFlow 付费档提供 A 股数据（无需注册、无需 API Key、无需本地客户端）。

## 一、能力对照

| 数据集 | 数据源 | 说明 |
|--------|--------|------|
| realtime（实时行情） | akshare | 全市场快照（新浪） |
| daily（日K） | akshare / baostock | 不复权原始价 |
| minute（分钟K） | akshare | 仅近期数据（新浪） |
| adj_factor（除权因子） | akshare / baostock | 官方分红送配推导 |
| depth5（五档盘口） | akshare | 腾讯直连 |
| financial（财务） | akshare | 新浪三表 + 指标 |

未接入：`full_minute`（全市场分钟线，免费源逐标的拉不动）。

## 二、前置依赖

- `uv`（Python 依赖管理）
- `pnpm`（前端依赖）

首次启动 `.\dev.ps1` 会自动安装后端/前端依赖。

## 三、安装步骤

### 1. 启动面板

```powershell
cd tick-stock-panel
.\dev.ps1
```

### 2. 安装 akshare / baostock 依赖

一键脚本（Windows PowerShell）：

```powershell
.\scripts\setup-akshare.ps1
```

或手动：

```powershell
uv pip install --python backend\.venv\Scripts\python.exe akshare baostock
```

### 3. 切换数据源

打开 http://localhost:3011 → **设置 → 数据源 Beta**，把以下数据集切到 `akshare`：

- 实时行情 → akshare
- 日K → akshare（或 baostock）
- 分钟K → akshare
- 复权因子 → akshare（或 baostock）
- 五档盘口 → akshare
- 财务 → akshare

### 4. 同步数据

数据需逐标的同步（免费源接口按股票查询，全市场同步不现实）。编辑 `backend/sync_watchlist_data.py` 中的 `SYMBOLS` 列表，然后运行：

```powershell
cd backend
.\.venv\Scripts\python.exe sync_watchlist_data.py
```

该脚本会同步所选股票的日K之外的财务三表、指标与除权因子。日K 则在页面「数据」页或用 `/api/kline/sync_batch` 同步。

### 5. 添加自选股、配置监控规则

- **自选股**：页面「自选股」添加，或调 `POST /api/watchlist/batch`。
- **监控规则**：页面「监控中心」新建（如涨跌幅、成交额、连板等阈值规则）。

## 四、网络注意事项

- 本项目插件**已内置**国内源与代理绕过逻辑（东财接口不可达时走新浪/腾讯；请求直连不走系统代理），无需额外配置。
- 安装依赖时若 PyPI 慢，可用国内镜像：`uv pip install --python ... --index-url https://mirrors.cloud.tencent.com/pypi/simple akshare baostock`。

## 五、已知局限

1. **逐标的拉取**：日K/财务/除权因子均为单股接口，全市场同步要数小时，建议只同步自选股。
2. **分钟K 仅近期**：新浪源约近 10 个交易日，无全量历史。
3. **异动监控**：需全市场日K 计算偏离值，免费源喂不饱，该功能建议用 TickFlow 付费档或 fuyao。
4. **财务字段**：新浪三表字段比 TickFlow 少，部分指标缺失。
