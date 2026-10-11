# 远端网关与 Codex 插件改造方案（设计记录）

> **状态：设计已定，尚未实现。** 本文记录的是**决策与约束**，不是已完成的接口。
> 「现状」段落一律给出仓库内文件位置，可复核；「目标」段落是待实现契约，
> 不得据此虚构当前代码中存在的 API（见 `AGENTS.md`、`docs/secondary-development.md` §1）。

## 0. 与其他文档的关系

| 文档 | 关系 |
| --- | --- |
| [`open-platform-plan.md`](open-platform-plan.md) | 开放网关（Token + scope + 限流 + Tier A/B）的**现状**定义。本文在其之上做多租户化，**不推翻**其既有契约。 |
| [`codex-plugin-and-remote-gateway.md`](codex-plugin-and-remote-gateway.md) | Codex 插件规范与远端 MCP 支持的一手来源调研记录。本文的插件形态结论以该文为证据。 |
| [`secondary-development.md`](secondary-development.md) | 扩展点分级（L1/L2/L3）与「不得虚构 API」的判定口径，本文沿用。 |
| [`plugin-development.md`](plugin-development.md)、[`custom-data-source.md`](custom-data-source.md) | 数据源 Provider 与 `plugin.yaml` 机制的现状。本文的 D2 会改变这些能力**运行在网络哪一侧**。 |
| [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | 数据契约、缓存、数据源中立性与验证矩阵的**唯一权威**。本文与它冲突时以它为准。 |

## 1. 目标与非目标

**目标**

1. 本系统可作为 **Codex 插件**安装，用户在 Codex 内使用现有系统能力。
2. **LLM 调用与行情调用全部改走远端服务**；用户登录后按其订阅获得相应服务。
3. 登录与登录后的**授权**覆盖两个面：行情数据获取、LLM 调用。
4. 插件构建参考 `zhongerxin/iPhone-use` 的打包与注册模式。

**非目标（明确排除）**

- 不把面板改造为多租户 SaaS，不在面板数据层引入用户维度。
  `docs/open-platform-plan.md:16` 已把多租户列为非目标，本文继续保留这一边界。
- 不做面板端的用户注册/支付/账单 UI。订阅与额度只存在于网关侧。
- 不改写行情数据的历史口径。远端化只改**数据的来源**，不改**口径的定义**。

## 2. 术语

**面板（Panel）** — 本仓库的现有应用（`backend/` + `frontend/`）。单用户、本地部署，
认证是单密码（`backend/app/services/auth.py:12`：「单密码, 不做多用户(避免重构全项目数据层)」）。

**网关（Gateway）** — 新增的远端服务。它同时是行情代理与 LLM 代理，是**唯一**持有
上游凭据（数据源 key、LLM key）的地方。面板不再持有这类凭据。

**档位（Tier）** — 订阅的等级。档位决定**能拿到什么**：哪些数据集、哪些 LLM 模型、
以及各池的额度上限。档位定义是一张**配置表**，具体档数与数值在本阶段不定。

**订阅（Subscription）** — 某个账号当前生效的档位及其有效期。

**双池计量（Two-pool metering）** — 行情额度按「历史池 / 实时池」分开计。
两者成本结构不同（历史数据可缓存复用，实时快照不可），合成一个池会让定价和限流都失真。

**票据（Ticket）** — 网关签发的短时效凭证，承载 `{账号, 档位, 作用域, 过期}`。
面板用它换取对上游的访问；网关侧以 HMAC 校验，不依赖服务端会话表。

**复权口径基准（Adjust basis）** — 前复权价所依赖的除权因子口径。
现状中**唯一**被显式编码的基准标记是分钟 K 的 `data/kline_minute/.raw_basis`
（`backend/app/services/minute_adjust.py:11`、`:35`；写入时机见
`backend/app/api/kline.py:1362`：「全部分区成功后创建 .raw_basis 标记: 此后读取自动复权投影、拉取改取原始价」）。

**口径指纹（Basis fingerprint）** — 本文新增的概念，见 D7。
它标记「这份派生数据是用哪套口径算出来的」，区别于现有 generation token 只标记
「内容变过没有」。

> ⚠️ **命名冲突（已存在的坑，不是本文引入的）**
> 仓库里有两个同名不同义的 `capabilities` 模块：
>
> | 模块 | 内容 | 语义 |
> | --- | --- | --- |
> | `backend/app/tickflow/capabilities.py` | `Cap`、`CapabilityLimits`、`CapabilitySet`、`CapabilityDenied` | **运行时探测**上游当前授予了什么 |
> | `backend/app/data_providers/capabilities.py` | `CAPABILITY_REGISTRY`、`build_capability_matrix()` | **设置页展示**的能力路由矩阵 |
>
> 本文提到的「能力矩阵」**一律**指后者。涉及前者时写全 `tickflow.capabilities`。
> 远端化会同时触碰这两个模块，混用名称会让改动范围完全判断错。

## 3. 决策记录

### D1 — 面板保持单用户、本地；多租户只落在新网关

**背景.** 需求要求「登录后按订阅获得服务」。最直白的实现是把面板本身改造成多用户系统，
但那意味着数据层、缓存、策略存储、自选、任务队列全部要引入用户维度。

**决定.** 改造范围止于**新增的网关**。面板维持单用户本地形态，
它相对网关只是一个**普通客户端**，与任何其他客户端没有结构性差别。

**理由.** 面板数据层没有任何用户维度：`backend/app/services/auth.py:12` 明确记录
「不做多用户(避免重构全项目数据层)」；`services/api_tokens.py` 的 token 记录字段里
没有 owner，也没有 TTL。把多租户压在面板上，等于重写 `repository.py`、
`storage`、`strategy` 全链路，而这些改动**不产生**需求 1–3 的任何价值。

**后果.** 面板的认证仍然只有单密码；面板的「登录」是**登录网关**，不是登录面板。
网关成为唯一需要多用户数据模型的地方，也因此是唯一需要数据库的地方。

---

### D2 — 数据源插件体系上移到服务端；面板侧只保留一个远端 Provider

**背景.** 现状是每次行情调用都在面板进程内完成：`data_providers/` 按
`CAPABILITY_REGISTRY` 路由到具体 provider，插件（akshare / baostock / fuyao / tickflow）
在面板进程内加载并直接出网。CONTRIBUTING §4 要求「任何通用功能都必须通过 provider 能力和
标准化数据集访问数据」，能力矩阵是路由的**单一权威**。

**决定.** 把 provider 插件体系**整体**迁到网关，由网关承担能力矩阵与路由。
面板侧只新增**一个**远端 provider，它把所有数据集请求转发给网关。

**理由.** 数据集边界（`daily` / `adj_factor` / `minute` / `full_minute` / `realtime` /
`depth5` / `financial`）与 fail-closed 规则已经是清晰的契约，
**它是可搬运的**。搬运点选在数据集边界，意味着面板内的 `repository`、
`indicators`、`strategy`、`backtest` 全部无需改动。

反过来，如果把路由留在面板、只把上游 key 换成网关代理，则面板仍需知道
「哪个数据集由谁提供」，能力矩阵会被复制成两份并开始漂移——这正是
CONTRIBUTING §4 要防的事。

**后果.**

- 面板的 `data_providers/` 从「多个 provider + 能力矩阵」退化为「一个远端 provider」。
  这是**减少**代码，不是增加。
- 上游凭据（akshare 无需 key，tickflow/fuyao 需要）全部移出面板，
  符合 CONTRIBUTING §8「不提交密钥、Token」的部署形态。
- **能力层中立是硬约束**（CONTRIBUTING §4）：档位、订阅、额度这些网关词汇
  **不得**出现在面板的通用界面里。它们只允许出现在「账号/订阅」设置区。
  通用行情界面看到的仍然只是「某数据集可用/不可用」。
- **实时指数不走能力矩阵**：`backend/app/services/index_const.py` 定义的四只核心指数
  是产品级固定契约（该文件自述「单一权威」，且「不开放配置」），
  远端化**不得**把它变成按档位可变的集合。

---

### D3 — 复权口径由服务端自算为权威，不下发上游推算因子

**背景.** 复权是三层结构（`backend/app/services/minute_adjust.py` 顶部注释）：
事实层存原始价 / 因子层存单事件比值 / 投影层存前复权价。
上游（tickflow、fuyao 等）也提供各自的前复权序列。

**决定.** 网关只下发**事实层**（原始价）与**因子层**（单事件比值，非累计），
**投影层**（前复权价）由消费端按 CONTRIBUTING §3.2 的统一公式自行计算。
网关不得下发上游已经推算好的前复权价。

**理由.** 上游们的前复权算法彼此不保证一致，且会随其内部调整而变。
一旦把上游前复权价当权威，同一份 `adj_factor` 在不同时间同步会得到不同的投影价，
而系统里**没有任何字段**能表达「这次的结果换口径了」。自算投影层是目前唯一
能保证跨源一致的做法。

**后果.** 网关的 `adj_factor` 数据集契约必须明确「单事件比值、非累计」，
并拒绝以任何形式透传上游的累计因子或前复权价。这是**契约收紧**，
需要在网关侧做显式校验而不是默认信任上游。

---

### D4 — 计量模型：LLM 按 token，行情按月、双池分离

**背景.** 两类资源的成本结构完全不同：LLM 按消耗量线性计费；
行情是一次性取数后近似零边际成本，但**实时**类不可缓存、按次计成本。

**决定.**

- LLM：按 **token** 计量（输入 + 输出分开记）。
- 行情：按 **月** 计量，且**历史池与实时池分开**，各自有独立上限。
- 档位配置表同时给出三组数值：LLM token 额度、历史池月度额度、实时池月度额度。

**理由.** 合成单一「行情额度」会让重度历史回测（便宜、可缓存）与
重度实时盯盘（贵、不可缓存）互相挤占，用户无法据此选档。
分开后档位差异可以真实反映成本差异。

**后果.** 网关的计量记录必须带**池标签**，且历史/实时走不同的计数桶。
面板侧的现有 Tier A/B 契约（`docs/open-platform-plan.md`）需要映射到新的池语义，
映射表在 `/to-spec` 阶段产出。

---

### D5 — 无状态网关：HMAC 票据 + DB 权威计数

**背景.** 现状的限流器、SSE 一次性票据、事件总线、长任务限流器**全部在进程内**，
且部署为单进程（未启用多 worker）。要横向扩容，这些假设会同时失效：
限流变成 N 倍宽松，跨进程票据直接 401。

**决定.** 网关侧：

- **认证/授权**用 HMAC 签名的**无状态票据**（含账号、档位、作用域、过期），
  任何实例都能独立校验，不依赖粘性会话。
- **计量与限流**以**数据库为权威计数源**，不以进程内计数器为准。

**理由.** 这是让「多实例」与「额度准确」能同时成立的最低成本路径。
把计数留在进程内，横向扩容就变成一个静默的正确性缺陷——它不会报错，
只会让用户超用。

**后果.** 网关需要一个真实数据库（面板侧可以继续没有）。
`services/api_tokens.py` 的 token 记录需要补 `owner` 与 TTL 字段才能承载多租户，
这是既有开放网关资产的**扩展**，不是替换。
所有「进程内单例」的既有实现（限流器、票据、事件总线）在网关侧都不能复用，
必须重新按无状态前提实现。

---

### D6 — 插件交付：一个插件、两个 MCP server、仅 streamable-http、双凭据

**背景.** Codex 插件以 `plugin.json` 声明，可捆绑 MCP server。
一手调研确认三条硬约束：

- 远端 MCP 只支持 **`streamable-http`**，**不支持 `sse`**（`sse` 在 schema 里存在，但 CLI 不认）。
- 远端端点**必须 HTTPS**（仅 localhost / 127.0.0.1 允许 HTTP）。
- 插件侧认证只有 `api_key`（bearer / basic / custom_header）与 `oauth` 两类，
  且**没有 `clientSecret` 字段**。

**决定.**

- 交付**一个** Codex 插件，内含**两个** MCP server：行情服务与 LLM 服务。
  （两者授权面不同、计量池不同，分成两个 server 才能各自声明凭据与作用域。）
- 只使用 `streamable-http`；网关必须提供 HTTPS 端点。
- 凭据同时支持 `api_key` 与 `oauth`。OAuth 必须按**公共客户端**设计（PKCE，
  或支持 DCR / CIMD），因为清单里没有放 client secret 的地方。

**理由.** 两个 server 的拆分直接对应 D4 的双池与需求 3 的两个授权面。
凭据双轨是必需的：`api_key` 覆盖脚本与自动化，`oauth` 覆盖交互式登录。
把 OAuth 设计成机密客户端会在清单层就写不下去。

**后果（含一个未闭合的技术问题）.** 插件捆绑的远端 MCP 条目
（manifest 的 `mcpServers`）与用户在 `~/.codex/config.toml` 里注册的条目
（`codex mcp add --url`）是**两层不同配置**。调研**未能确认**
Codex 运行时是否真的会采纳插件捆绑的远端条目。

因此：**必须双重保险** —— 清单里声明远端 server，**同时**在安装步骤里
显式执行 `codex mcp add`。在任何一次真机验证证明清单足以生效之前，
不得省略第二步。这是一条实现期必须实测收口的事项，已记入 §6。

---

### D7 — 复权口径指纹：显式编码「用哪套口径算的」

**背景.** 现有失效机制解决的是「内容变没变」，不是「口径换没换」：

- 内容 generation token（`backend/app/services/enriched_generation.py`）在
  enriched 重算后推进，缓存据此判断内容已更新。这是正确的，也是原子的。
- 但**如果因子口径变了而 enriched 没有重写**，`source_generation` 不变，
  回测矩阵磁盘缓存会认为自己的内容仍然匹配，于是**继续供应旧口径的结果**。
  该缓存的版本校验只在实现版本不匹配时抛错
  （`backend/app/backtest/matrix.py:1714-1715` → `ValueError("matrix disk cache version mismatch")`；
  版本常量见同文件 `:53` `_MATRIX_CACHE_VERSION`、`:54` `_DIRECT_MATRIX_LOADER_VERSION`），
  而它校验的是 loader 实现版本，**不是**复权口径。
- 全仓库唯一的例外是分钟 K 的 `.raw_basis` 标记
  （`backend/app/services/minute_adjust.py:11`、`:35`）。日 K、enriched、回测矩阵
  **都没有**等价机制。

**决定.** 为派生层引入**显式的复权口径指纹**：因子口径一旦变化，
派生数据（enriched、回测矩阵缓存）必须能被判定为失效，**不依赖**内容 token 是否推进。
实现形态沿用 `.raw_basis` 的既有先例——落一个可读的基准/指纹标记，
读侧先校验指纹、不匹配即 fail-closed 重算。

**理由.** 这是本次改造中**唯一**会静默产生错误金融结果的缺陷面：
它不报错、不崩溃，只是让用户按旧口径回测出新口径的结论。
CONTRIBUTING §12 明确把「混用复权价」列为不得合并的情况。

**后果.** 需要一次**批量回算**把指纹落到既有派生数据上。
按 CONTRIBUTING §13，批量数据覆盖属于高风险操作，**执行前必须再次单独确认**（见 §7）。

---

## 4. 目标架构（形态，非接口）

```
Codex ──MCP(streamable-http, HTTPS)──┐
                                     ├──> 网关 ──> 上游数据源（provider 插件）
面板（单用户本地）──远端 provider────┘        └──> 上游 LLM
```

- 网关是唯一持有上游凭据、唯一具备用户模型、唯一有数据库的一侧。
- 面板与 Codex 是**同级客户端**，走同一套授权与计量。
- 面板内的数据流、口径、指标、策略、回测**保持不变**；变的只是数据从哪里来。

## 5. 现状地基与缺口

**可直接复用的地基**

| 资产 | 位置 | 说明 |
| --- | --- | --- |
| 开放网关骨架 | `backend/app/services/api_gateway.py` | Bearer 校验 + scope 规则表 |
| Token 体系 | `backend/app/services/api_tokens.py` | `tsp_` 前缀、仅存 SHA-256 哈希 |
| MCP 桥 | `mcp-server/`（`tsp_mcp_server`） | 现有 stdio 薄桥，可演进为远端形态 |
| LLM 配置已被外部化 | `backend/app/config.py:86-90` | `ai_provider` / `ai_base_url` / `ai_api_key` / `ai_model` 均可配置，网关几乎是零改动接入 |
| 复权自算链路 | `backend/app/indicators/pipeline.py` | 前复权投影已在本地计算，符合 D3 |
| 口径标记先例 | `backend/app/services/minute_adjust.py:11`、`:35` | D7 要照抄的形态 |

**已知缺口（均需在网关侧新建）**

- Token 记录只有 `id / name / token_hash / scopes / created_at / last_used_at / revoked`，
  **无 owner、无 TTL** → D5 的前置改造。
- 全仓库无 `tenant` / `subscription` / `billing` / `user_id` 的任何命中。
- LLM 侧只有一个**出站**客户端，没有任何代理或转售端点。
- 限流器、票据、事件总线、长任务限流器全为进程内单例，且部署为单进程
  （未启用 `--workers`） → 横向扩容会静默破坏限流与跨进程票据。

**顺带记下的既有缺陷（非本次引入）**

`docs/open-platform-plan.md:91` 声称存在 `OPEN_API_CORS_ORIGINS` 白名单，
但实际代码是 `backend/app/main.py:417-423` 的 `allow_origins=["*"]`，架在一个
**需要认证的 API** 上。文档与实现不一致，且这个不一致会让后续设计误判安全边界。
是否在本改造中一并处理，留待 §6 未决项。

## 6. 未决事项

1. **插件捆绑的远端 MCP 条目是否被运行时采纳** —— 必须真机实测收口（见 D6）。
2. **档位的档数与每档数值** —— 由配置表承载，具体数字推迟到 spec 之后。
3. **既有 Tier A/B 契约到双池语义的映射表** —— `/to-spec` 阶段产出。
4. **CORS 文档/实现不一致是否在本改造中修** —— 需单独决策，不应顺手夹带。

## 7. 执行期风险与必须二次确认的操作

按 CONTRIBUTING §13，以下操作**不自动执行**，各自需要一次独立确认：

- **全市场批量回算 enriched**（D7 的落地动作）。现有唯一不取数、只重算的入口是
  `POST /api/kline/rebuild_enriched`（`backend/app/api/kline.py:1582`），
  其 docstring 自述「全量重算 enriched 表 —— 不获取任何数据, 仅基于已有
  kline_daily + adj_factor 重算复权+指标。」；底层 `run_pipeline()` 在全量模式下
  拒绝覆盖缺失的既有日期分区（`backend/app/indicators/pipeline.py`：
  「全量重建结果缺少已有日期分区,拒绝覆盖」）。
  回归锚点已存在：`backend/tests/test_pipeline_capacity.py:155`。
- **移除 `codex_cli` provider**（`backend/app/config.py:90` `ai_codex_command`）——
  LLM 调用全部远端化后该路径的处置需单独确认。
- **会话搬迁**（现有面板内对话迁到 Codex 侧）—— 落地前单独确认。

**性能注意.** 全市场 enriched 重算缺少直接实测数字。已知的量级参照
（均来自仓库注释与文档，非本次测量）：compute_indicators 在 107 万行下
低配机 50s+；延长历史后全市场约 800 万行；日线全量同步的成本见
`docs/akshare-setup.md` 对单股接口的说明。**不得**把这些参照当作
enriched 全量重算的实测耗时（CONTRIBUTING §13：不虚构性能数据）。

## 8. 参考

- [`codex-plugin-and-remote-gateway.md`](codex-plugin-and-remote-gateway.md) —— Codex 插件清单、
  远端 MCP 支持、`iPhone-use` 打包模式的一手调研记录。
- [`open-platform-plan.md`](open-platform-plan.md) —— 开放网关现状与非目标。
- [`../CONTRIBUTING.md`](../CONTRIBUTING.md) —— §3 数据契约、§4 数据源插件化与能力中立、
  §8 安全与用户数据、§12 不得合并的情况、§13 AI 编码代理规则。
- [`secondary-development.md`](secondary-development.md) —— 扩展分级与「不得虚构 API」的口径。
