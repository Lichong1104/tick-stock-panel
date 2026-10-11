# Codex 插件打包与远程网关可行性调研

> 状态: 一手资料调研 (2026-10-11)。**未修改任何代码/测试/配置**, 本文是唯一产出。
> 标的决策: 面板 (a) 打包为 Codex 插件, (b) 作为「转售行情 + LLM 调用的远程网关」的客户端, 以用户登录 + 订阅为门槛; 面板自身保持单用户/本地。
> 全文区分 **【已证实】** (来自一手来源, 附 URL 或 file:line) 与 **【推断】**。无法从一手来源确认的内容集中在末节「未能确认的事实」。

## 结论摘要

**决定性问题 2 的答案: Codex 插件可以指向远程 MCP 服务, 不是只能跑本地 stdio 子进程。【已证实】**

两条独立的一手证据:

1. **规范层**: 插件 `plugin.json` 引用的 Agent Plugins 1.0.0 MCP schema 中, server 定义是 `oneOf: [stdioServer, streamableHttpServer, sseServer]`;`streamableHttpServer` 必填 `type: "streamable-http"` + `url`, 可选 `headers`。
   来源: `https://agent-plugins.org/schemas/1.0.0/mcp.schema.json` (`$defs.server`, `$defs.streamableHttpServer`)。
2. **OpenAI 文档层**: 官方打包文档直接给出远程写法:
   ```json
   "mcpServers": { "docs": { "type": "streamable-http", "url": "https://example.com/mcp" } }
   ```
   并说明公开提交时「把远程 HTTPS 端点通过 **With MCP** 提交」, 本地服务「必须部署到公网 HTTPS URL」。
   来源: *Package your plugin* — `https://developers.openai.com/plugins/build/plugins`。

但有一个必须记住的分界线: **Streamable HTTP 支持, 旧版 HTTP+SSE 不保证**。Codex CLI 的 `config.toml` 文档只列 STDIO 与 Streamable HTTP 两种传输, `sse` 未被列为支持的传输 (来源: *Model Context Protocol* — `https://learn.chatgpt.com/docs/extend/mcp?surface=cli`)。虽然 Agent Plugins schema 里仍有 `sse` 变体, 新设计**应只使用 `streamable-http`**。

对本项目最重要的一条**残余不确定性**【推断】: 官方文档所描述的是**包格式**, 而 Codex CLI 在运行时是否会把「插件自带的远程 MCP 条目」与「`config.toml` 里手工注册的远程 MCP」一视同仁, 一手来源没有明说。参考实现 iPhone-use 的作者明确**绕开**了插件自带的 MCP 注册 (见 §3), 理由写在代码注释里 —— 这说明插件自带 MCP 在 Codex 中另有行为差异。因此建议采用「双保险」: `mcp.json` 里声明远程 URL, **同时**提供一个 `codex mcp add <name> --url ...` 的安装步骤 (§3 的 `register_mcp.py` 就是现成范式)。

另外, 官方插件规范里已内置了「登录 + 订阅」所需的两条认证通道: 每个 MCP server 可声明 `auth` 为 `api_key` (bearer/basic/custom_header) 或 `oauth` (含 DCR / CIMD 客户端注册), CLI 侧对应 `codex mcp login <server-name>`。**这直接就是订阅门槛的落点**【已证实】。

---

## 1. Codex 插件打包规范

### 1.1 两种被接受的目录形态

**【已证实】** 官方文档接受两种清单, 两者可共存, 便携格式是权威:

| 形态 | 清单位置 | 说明 |
| :--- | :--- | :--- |
| 便携 (portable) | 根目录 `plugin.json` | `$schema` 指向 agent-plugins.org; 组件从根目录的 `skills/`、`mcp.json`、`assets/` 发现 |
| 兼容覆盖 (compat overlay) | `.codex-plugin/plugin.json` | "Existing `.codex-plugin/plugin.json` files remain supported as a compatibility fallback"; `@plugin-creator` 脚手架生成的就是这个 |

便携格式的根目录构件: `plugin.json`、`skills/`、`mcp.json`、`assets/`。OpenAI 专属设置写在根 `plugin.json` 的 `extensions.com.openai` 下; 用覆盖层时, `.codex-plugin/` 里**只保留 `plugin.json`**, 被引用的 hooks / `.app.json` 等仍留在插件根。

关键行为: `extensions.com.openai` 内联对象存在时, **完全取代** `.codex-plugin/plugin.json` 覆盖层, 二者不合并 (官方原文: "replaces the entire `.codex-plugin/plugin.json` overlay as the source of OpenAI-specific settings; the two aren't merged")。

来源: *Package your plugin* (`developers.openai.com/plugins/build/plugins`)。

### 1.2 根 `plugin.json` 的字段 (便携格式, 由 schema 锁定)

**【已证实】** `https://agent-plugins.org/schemas/1.0.0/plugin.schema.json` 是机器可读的权威约束:

- 必填: `$schema` (`const` 为 `https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`)、`name`
- 允许: `$schema` `name` `version` `description` `author{name,email,url}` `homepage` `repository` `license` `keywords` `extensions`
- `additionalProperties: false` — **根清单不允许出现表外字段**
- `name` 约束: `^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$`, 最长 64, 即小写 kebab-case

注意这里的一个歧义点: 官方 `openai/skills` 仓库的样例 spec (`skills/.system/plugin-creator/references/plugin-json-spec.md`) 把 `skills` / `mcpServers` / `apps` / `hooks` / `interface` 列为**顶层字段**, 但该文档自己说明「本仓库脚手架写 `.codex-plugin/plugin.json`」—— 即那套顶层形状描述的是**兼容覆盖层**, 而非受 agent-plugins schema 约束的便携清单。参考实现的两种清单正好印证了这个分工 (§3.2)。

OpenAI 专属字段 (`extensions.com.openai` 内, 以及兼容覆盖层): `apps`、`hooks`、`onboardingSkill`, 以及 `interface{displayName, shortDescription, longDescription, developerName, category, capabilities[], websiteURL, privacyPolicyURL, termsOfServiceURL, defaultPrompt[], brandColor, composerIcon, logo, screenshots[]}`。

`interface.defaultPrompt` 的硬约束 (来自 `plugin-json-spec.md`): 最多 3 条, 超出忽略; 每条上限 128 字符, 超出截断; 建议 ~50 字符。`screenshots` 必须是 PNG 且存放在 `./assets/` 下。

### 1.3 `skills/` 结构

**【已证实】** 布局为 `skills/<skill-name>/SKILL.md`, YAML frontmatter 至少含 `name` 与 `description`, 正文在 frontmatter 之后。便携格式**不需要**在清单里声明 `skills` 字段 —— 官方原文: portable packages "discover skills from the root `skills/` directory"; 兼容覆盖层则用 `"skills": "./skills/"` 显式指向。

**【推断】** 技能在插件内按 `{plugin_name}:{skill_name}` 命名空间暴露 (搜索结果如此描述, 但我在官方打包文档页面上未找到该句原文, 故标为推断)。

可选: 设 `skills/setup/SKILL.md` 并把 `extensions.com.openai.onboardingSkill` 指到 `"./skills/setup/SKILL.md"` (路径相对插件根, 必须指向已打包的技能)。

### 1.4 注册与安装

**【已证实】** Marketplace 文件位置 (ChatGPT 桌面端读取的三处):

- 仓库级: `$REPO_ROOT/.agents/plugins/marketplace.json`
- 个人级: `~/.agents/plugins/marketplace.json`
- 旧兼容: `$REPO_ROOT/.claude-plugin/marketplace.json`

`marketplace.json` 顶层含 `name`、可选 `interface.displayName`; `plugins[]` 为**渲染顺序**。每个条目**必须**含 `policy.installation`、`policy.authentication`、`category`:

```json
{
  "name": "iphone-use",
  "source": { "source": "local", "path": "./dist/iphone-use" },
  "policy": { "installation": "AVAILABLE", "authentication": "ON_INSTALL" },
  "category": "Productivity"
}
```

- `policy.installation` ∈ `NOT_AVAILABLE` | `AVAILABLE` | `INSTALLED_BY_DEFAULT` (新条目默认 `AVAILABLE`)
- `policy.authentication` ∈ `ON_INSTALL` | `ON_USE` (新条目默认 `ON_INSTALL`)
- `policy.products` 是显式覆盖项, 非明确要求应省略
- `source.source` 支持 `local` (配 `path`, 相对 marketplace 根, `./` 开头)、`url` (插件在仓库根)、`git-subdir` (配 `url`/`path`/`ref`|`sha`)、`npm` (配 `package`, 可选 `version`/`registry`; 需本机有 npm, 且不跑 lifecycle 脚本)
- 无法解析的条目会被跳过, 不会让整个 marketplace 失败

来源: 上述两页 + `openai/skills` 的 `plugin-creator/SKILL.md` 与 `references/plugin-json-spec.md`。

**【已证实】** CLI (`codex plugin …`): `marketplace add <local-path | owner/repo[@ref] | https-git | ssh-git>` (支持 `--ref`、`--sparse`)、`marketplace list`、`marketplace upgrade [name]`、`marketplace remove <name>`、`plugin add <plugin>@<marketplace>`、`plugin list --json`、`plugin remove`。官方 PR: `openai/codex` #21396 "add plugin marketplace CLI commands"。旧版本 (<0.122) 命令为 `codex marketplace add`。

**【已证实】** 安装后缓存在 `~/.codex/plugins/cache/$MARKETPLACE_NAME/$PLUGIN_NAME/$VERSION/`; 本地插件 `$VERSION` 为 `local`; 应用加载的是缓存副本而非 marketplace 条目。启停在 `~/.codex/config.toml`:

```toml
[plugins."my-plugin@local-repo"]
enabled = true
```

插件级 MCP 策略也在 Codex 配置里 (不在包内):

```toml
[plugins."my-plugin".mcp_servers.docs]
enabled = true
```

并支持 `default_tools_approval_mode`、`enabled_tools`、逐工具 `approval_mode`。

### 1.5 Hooks (本项目大概率用不到)

**【已证实】** 默认发现 `hooks/hooks.json`, 条目形如 `SessionStart` → `hooks[]` → `{type: "command", command: "...${PLUGIN_ROOT}...", statusMessage: "..."}`。插件 hooks 会拿到 `PLUGIN_ROOT` (安装根) 与 `PLUGIN_DATA` (可写数据目录), 另有 `CLAUDE_PLUGIN_ROOT` / `CLAUDE_PLUGIN_DATA` 兼容名。

**重要限制**: hooks「仅支持手动安装的 Codex 桌面插件」, **不具备公开提交资格**, 且在用户审阅并信任前会被跳过; 从 web 安装不会部署 hook 脚本。

---

## 2. 关键约束: 插件能否指向远程 MCP 服务

### 2.1 结论: 能 —— 规范与官方文档都支持远程 HTTP

**【已证实 · 规范】** `mcp.json` 的 schema 对 server 使用 `oneOf`:

| 变体 | 必填 | 可选 | 备注 |
| :--- | :--- | :--- | :--- |
| `stdio` | `type` = `"stdio"`, `command` | `args`, `env`, `cwd` | `env` 禁止覆盖 `PLUGIN_ROOT`/`PLUGIN_DATA`; `cwd` 必须以 `./` 或 `${PLUGIN_ROOT}`/`${PLUGIN_DATA}` 开头 |
| `streamable-http` | `type` = `"streamable-http"`, `url` | `headers` | 远程服务用这个 |
| `sse` (legacy) | `type` = `"sse"`, `url` | `headers` | 规范里仍存在, 但 Codex 运行时未列为支持 |

来源: `https://agent-plugins.org/schemas/1.0.0/mcp.schema.json`。

顶层还必须含 `$schema` (const 指向该 mcp schema) 与 `mcpServers`。

**【已证实 · OpenAI 文档】** 远程写法与提交要求:

```json
"mcpServers": { "docs": { "type": "streamable-http", "url": "https://example.com/mcp" } }
```

- "For public submission, submit the remote HTTPS endpoint through **With MCP**; a local server must be deployed to a public HTTPS URL, otherwise contact your OpenAI contact for local MCP support."
- 文档叙述的 server 字段为 `type`、`url`、`extensions` —— **与 schema 的 `headers` 不一致**, 这是一个应当以 schema 为准、并在实测中确认的细节 (见末节)。

来源: *Package your plugin*。

### 2.2 Codex CLI 侧的传输支持

**【已证实】** `config.toml` 的 `[mcp_servers.<name>]`:

- STDIO: `command` (必填)、`args`、`env`、`env_vars`、`cwd`、`experimental_environment = "remote"` (通过远程执行环境启动 stdio)
- Streamable HTTP: `url` (必填)、`auth` (默认 `oauth`; `chatgpt` 表示用当前 ChatGPT 会话, 仅限受信任的一方 ChatGPT 源)、`bearer_token_env_var`、`http_headers` (静态头映射)、`env_http_headers` (头名 → 环境变量名)、`http_headers_helper` (本机命令输出 JSON 头; 仅限本地 HTTP 连接)
- 共有: `startup_timeout_sec` (默认 10)、`tool_timeout_sec` (默认 60)、`enabled`、`enabled_tools` / `disabled_tools` (拒绝表在允许表之后生效)、`default_tools_approval_mode` ∈ `auto|prompt|writes|approve`、`tools.<tool>.approval_mode` / `tools.<tool>.output_token_limit`

**SSE 未被列为支持的传输**。官方示例 (节选): `url = "https://mcp.figma.com/mcp"` 配 `bearer_token_env_var = "FIGMA_OAUTH_TOKEN"`。

CLI: `codex mcp add <name> -- <stdio-cmd> [args]` (可带 `--env K=V`); 远程为 `codex mcp add example --url https://mcp.example.com --oauth-client-id my-client`; 另有 `codex mcp list`、`codex mcp get <name> --json`、`codex mcp remove <name>`、`codex mcp login <server-name>`, 以及 `--oauth-client-registration cimd|dcr` (默认 `auto`)。

一条与「面板本地、插件远程」直接相关的表述: "Local MCP servers and transports may not be available in the cloud" —— 即本地 stdio MCP 在云端编排下不保证可用, 远程 MCP 是云端路径的正解。

来源: *Model Context Protocol* — `https://learn.chatgpt.com/docs/extend/mcp?surface=cli`。

### 2.3 认证: 订阅门槛的落点

**【已证实】** 每个 MCP server 可在 `extensions["com.openai"].auth` 下声明四种之一:

- `"none"`
- `"api_key"`: `{ "type": "api_key", "headerScheme": "bearer" }` —— `headerScheme` 也接受 `basic` / `custom_header`; `custom_header` 需 `headerName` (禁用 `Host`)。文档注明这类声明**不被提交门户的连接表单支持**。
- `"oauth"`: 三种客户端注册模式 —— `provided` (配 `clientId`, 可选 `client.tokenEndpointAuthMethod` ∈ `client_secret_basic|client_secret_post|none|private_key_jwt`, 声明 `private_key_jwt` 不会配置签名密钥)、`dcr` (`{ "mode": "dcr" }`, 可选 `registrationUrl`)、`cimd` (`{ "mode": "cimd" }`)。`dcr`/`cimd` 对象中不允许出现 `clientId` 或客户端凭据; CIMD 偏好不会关闭 DCR 回退。
- `"mixed"`: 未认证 + OAuth 并存; **不能**把 OAuth 与固定 API key 组合。

`auth` 内可用的 OAuth 字段: `client`、`client.mode` (必填当 `client` 存在)、`client.clientId`、`client.tokenEndpointAuthMethod`、`authorizationUrl`、`tokenUrl`、`registrationUrl`、`authorizationServerBase`、`resource`、`baseScopes`、`defaultScopes`、`oidcEnabled`、`oidcConfigurationUrl`、`oidcUserinfoEndpoint`、`oidcScopesSupported`。字段名大小写敏感 (是 `client.clientId`, 不是顶层 `oauth.client_id`)。**不存在 `clientSecret` 字段** —— 密钥通过连接设置流程提供。

来源: *Package your plugin*。

对本项目: `api_key` + `headerScheme: "bearer"` 与现有 `Authorization: Bearer tsp_...` 心智模型一致, 适合做「订阅密钥」; 若要做真正的「用户登录」, `oauth` (尤其 DCR/CIMD) 是官方支持的通道, 且 CLI 有 `codex mcp login` 配套。

### 2.4 小结与建议

| 问题 | 答案 | 证据强度 |
| :--- | :--- | :--- |
| 插件能否声明远程 MCP? | 能, `type: "streamable-http"` + `url` | 【已证实】schema + 官方文档 |
| 只能局部 stdio 吗? | **否** | 【已证实】同上 |
| 支持 SSE 吗? | 规范里有 `sse` 变体, 但 Codex 传输文档未列 → 不要用 | 【已证实】文档层面 |
| 远程需要 HTTPS 吗? | 公开提交必须公网 HTTPS | 【已证实】 |
| 插件自带的远程 MCP 条目, Codex CLI 运行时一定生效吗? | 未明说, 需实测; 建议同时提供 `codex mcp add --url` 路径 | 【推断】 |

---

## 3. 参考实现: `github.com/zhongerxin/iPhone-use`

以下全部来自我直接抓取的仓库文件内容 (raw 原文), 非二手转述。抓取于 2026-10-11。

### 3.1 仓库结构

根目录清单: `.agents/plugins/`、`.codex-plugin/`、`.github/workflows/`、`assets/`、`scripts/`、`server/`、`skills/`、`tests/`、`tooling/`、`ui/`; 根文件: `.gitignore`、`.mcp.json`、`ANALYTICS.md`、`CHANGELOG.md`、`LICENSE`、`README.en.md`、`README.md`、`THIRD_PARTY_NOTICES.md`、`mcp.json`、`plugin.json`。

`scripts/package.py` 中 `FILES` 元组证实了插件清单集合: `plugin.json`、`mcp.json`、`.mcp.json`、`.codex-plugin/plugin.json`、`.agents/plugins/marketplace.json`; `DIRS` = `assets`、`server`、`skills`。

### 3.2 两种清单的实际分工

`plugin.json` (便携格式, 实测内容):

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "iphone-use",
  "version": "0.3.10",
  "description": "...",
  "author": { "name": "ZHONG XIN" },
  "license": "MIT",
  "extensions": {
    "com.openai": {
      "interface": {
        "displayName": "iPhone Use",
        "shortDescription": "Let Codex control your iPhone",
        "longDescription": "...",
        "developerName": "ZHONG XIN",
        "category": "Productivity",
        "logo": "./assets/icon.svg",
        "composerIcon": "./assets/icon.svg",
        "defaultPrompt": ["...", "..."]
      }
    }
  }
}
```

注意: 便携清单里**没有**顶层 `skills`/`mcpServers`/`interface`; 全部走 `extensions.com.openai`。

`.codex-plugin/plugin.json` (兼容覆盖层, 实测内容) 则是**扁平**的: 顶层有 `skills: "./skills/"`、`mcpServers: "./.mcp.json"`, 且 `interface` 直接放在顶层 (不在 `extensions` 下)。

`package.py` 的 `validate()` 用断言把这份对应关系钉死:

```python
assert ui == overlay['interface']
assert all(manifest[k] == overlay[k] for k in ('name','version','description'))
assert overlay['mcpServers'] == './.mcp.json' and overlay['skills'] == './skills/'
assert portable['mcpServers'] == legacy['mcpServers']
assert list(portable['mcpServers']) == ['iphone_use']
for config in portable['mcpServers'].values():
    assert config['type'] == 'stdio' and config['args'] == ['${PLUGIN_ROOT}/server/iphone_use.py']
```

**这条断言对本项目是负面信号**: 该参考仓库的校验器**把 `type` 硬编码为 `stdio`**, 因此它整体不是「插件指向远程服务」的模板, 只在清单形状、marketplace 生成、安装脚本这三件事上可复用。

### 3.3 MCP 声明与传输

`mcp.json` (便携) 与 `.mcp.json` (兼容)**内容完全相同**:

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
  "mcpServers": {
    "iphone_use": {
      "type": "stdio",
      "command": "python3",
      "args": ["${PLUGIN_ROOT}/server/iphone_use.py"],
      "cwd": "${PLUGIN_ROOT}"
    }
  }
}
```

**传输实况**: 面向 Codex 是 **stdio** (Codex 以 `python3` 子进程拉起 MCP 服务); 面向 WDA 是**本机 loopback HTTP**。README 原文:

- "Python MCP 服务通过本机 loopback 转发访问 WDA，运行数据和签名构建保留在本机"
- "`WDA_URL` 可指定本机地址，默认转发端口为 18100，**远程地址会被拒绝**"

即: **本地性约束是 MCP 服务自己在代码里强制的 (拒绝非本机 WDA 地址), 不是 MCP 配置层的限制**。这一点很关键 —— 它说明「MCP 声明为 stdio」与「被访问的后端只允许本机」是两个独立决策, 前者可以改成远程, 后者是他们的产品选择。

### 3.4 `scripts/install.sh` 逐步

实测全文 (4 步):

```sh
#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python3 "$task_root/scripts/package.py" --stage-only
codex plugin marketplace add "$task_root" --json
task_installation=$(codex plugin add iphone-use@iphone-use-local --json)
printf '%s\n' "$task_installation" | python3 "$task_root/scripts/register_mcp.py"
printf '%s\n' 'Reconnect the Codex chat to load iPhone Use tools and skills.'
```

1. `package.py --stage-only` —— 校验清单 + 把可发布文件复制到 `dist/iphone-use/` (排除 `__pycache__`/`node_modules`、`.pyc`、`.jsonl`、本地文档与依赖安装; 拒绝符号链接)
2. `codex plugin marketplace add "$task_root" --json` —— 注册本仓库为本地 marketplace
3. `codex plugin add iphone-use@iphone-use-local --json` —— 安装插件 (JSON 输出喂给下一步)
4. `register_mcp.py` —— 从安装结果里取 `installedPath`, 把缓存里的服务注册为标准 MCP

### 3.5 `scripts/register_mcp.py` 与「同一命名空间」

实测关键代码:

```python
root = Path(installation["installedPath"]).resolve(strict=True)
server = root/"server"/"iphone_use.py"
if not server.is_file():
    raise SystemExit("Installed iPhone Use MCP entrypoint is missing.")
# Same name as the plugin registration: Config wins over Plugin in Codex,
# so there is one namespace, without the shared agent-plugin tool budget.
subprocess.run(["codex","mcp","add","iphone_use","--","python3",str(server)], check=True)
```

**「避免重复工具注册的共享命名空间」的确切含义**: 插件注册名 (来自 marketplace) 与 `codex mcp add` 的注册名**都用 `iphone_use`**。注释给出的两条理由:

1. "**Config wins over Plugin in Codex**" —— 配置层注册优先于插件层, 因此同名不会产生两套工具, 只留一个命名空间;
2. "**without the shared agent-plugin tool budget**" —— 走配置层注册可以避开「agent-plugin 共享工具预算」。**【推断】** 这句话暗示: 由插件自带 `mcp.json` 注册的 MCP 工具会与其它插件共享一个工具数量/预算额度, 而配置层注册不受该额度约束。这个机制我在官方文档里没有找到对应说明 —— 它是作者的经验结论。

脚本还有一段 `retire_previous_registration()`, 做**只退役自己拥有过的旧注册**: 先 `codex mcp get iphone_wda --json`, 仅当 transport 为 `stdio`、参数路径位于 `cache/iphone-wda-local/iphone-use-wda` 下、且以 `server/iphone_wda.py` 结尾时, 才 `codex mcp remove iphone_wda`; 再把 `$CODEX_HOME/config.toml` 中 `[plugins."iphone-use-wda@iphone-wda-local"]` 段的 `enabled = true` 改为 `false` (正则改写 + `mkstemp` 同目录原子替换 + 保留原文件权限位), 且**故意保留旧缓存**以免正在运行的 setup worker 受影响。这是「升级路径不误删用户配置」的可用范式。

### 3.6 Skills

`package.py` 断言存在两个技能, 且 `SKILL.md` 必须以 `---\n` 开头并含 `name:`(精确匹配技能名) 与 `description:` 行: `skills/iphone-use/SKILL.md`、`skills/iphone-use-setup/SKILL.md`。README 提到技能内还有 `references/` 子目录 (如 `skills/iphone-use/references/screen.md`), 即技能目录可带附属参考文件。

### 3.7 Marketplace 条目

`.agents/plugins/marketplace.json` 实测全文:

```json
{
  "name": "iphone-use-local",
  "interface": { "displayName": "iPhone Use Local" },
  "plugins": [
    {
      "name": "iphone-use",
      "source": { "source": "local", "path": "./dist/iphone-use" },
      "policy": { "installation": "AVAILABLE", "authentication": "ON_INSTALL" },
      "category": "Productivity"
    }
  ]
}
```

注意 `source.path` 指向**打包产物** `./dist/iphone-use` 而非仓库根 —— 安装的是 `package.py` 裁剪后的副本。这与官方「缓存副本被加载, 而非 marketplace 条目」的描述一致。

### 3.8 可复用 / 不可复用

**对本项目可复用**:

- 双清单结构 (便携 `plugin.json` + 兼容 `.codex-plugin/plugin.json`) 与它们的字段分工, 以及 `package.py verify` 这种「用断言把两种清单钉在一起」的校验思路
- `skills/<name>/SKILL.md` + `references/` 的技能组织
- `.agents/plugins/marketplace.json` 的形状与 `policy` 缺省值
- 安装脚本三段式: 打包 → `codex plugin marketplace add` → `codex plugin add`
- `register_mcp.py` 的「同名注册 + 只退役自己拥有的旧注册 + 原子改写 config.toml」范式, 以及用 `codex mcp get --json` 读回真实 transport 做归属判断的思路
- `PLUGIN_ROOT` 占位符的用法 (让声明与安装路径解耦)

**不可复用**:

- `validate()` 对 `type == 'stdio'` 的硬断言 —— 远程网关方案必须改成 `streamable-http` + `url`
- 其 stdio-only 的 `mcp.json` / `.mcp.json` 内容
- 「拒绝非本机后端地址」的产品约束 (与远程网关目标相反)
- 「用配置层注册绕开插件工具预算」这个 workaround 的前提未经官方文档确认, 不应作为设计依赖

---

## 4. 本项目现有基础

### 4.1 现有开放网关 (Token + scope + 限流)

**【已证实 · 直接读源码, 已逐条复核】**

- **调用位置与顺序**: 网关不是独立中间件, 而是被 `main.py:460` 的 `auth_middleware` 调用的决策函数 `api_gateway.evaluate()`。顺序: 非 `/api/` 放行 → `OPTIONS` 放行 → 白名单放行 → **Bearer 分支** (`main.py:462-477`) → 否则密码/会话分支 (`main.py:479-499`)。Bearer 与密码会话是**并行**通道: 有 `Bearer ` 头就完全跳过密码路径 (`main.py:464` 用 `authz.startswith("Bearer ")`, 大小写敏感)。`evaluate()` 内部顺序为 验签 → 取 scope → scope 校验 → 限流 (`services/api_gateway.py:122-159`)。
- **规则表**: `_RULES` 是 `(method, 路径前缀, scope)` 三元组列表, **首匹配胜出** (`api_gateway.py:21-64`)。例如 `("GET","/api/kline/daily","read:market")`、`("GET","/api/strategies","read:analysis")`、`("POST","/api/screener/run","run:backtest")`、`("*","/api/paper","paper:trade")`。另有后缀表 `_SUFFIX_RULES` = `[("POST","/ingest","write:ext")]`, 仅作用于 `/api/ext-data/` 下 (`api_gateway.py:68-71`)。匹配用前缀 + 边界检查 (`path == prefix or path.startswith(prefix.rstrip("/") + "/")`), 未命中 → `None` → 403「未对外开放」。`"*"` scope = 仅需有效 Token (`api_gateway.py:135-136`)。有一处例外: `read:ext` 且路径以 `/api-key` 结尾时返回 `None` (管理面不外露)。
- **限流: 进程内、按 token id、固定 60 秒窗口**。`_BUCKETS: dict[str, deque[float]]` + `threading.Lock` (`api_gateway.py:103-105`), 键为 `record["id"]` (`api_gateway.py:145`)。**上限是一个全局环境变量** `OPEN_API_RATE_LIMIT_PER_MIN` (默认 120, 夹取 1~10000, `api_gateway.py:76-82`), **没有按 Token / 按套餐的差异化额度**。成功响应带 `X-RateLimit-Limit` / `X-RateLimit-Remaining`, 429 带 `Retry-After`。
- **无按 Token 的并发限制** (网关内不存在以 token 为键的信号量)。
- **令牌存储** (`services/api_tokens.py`): `data/user_data/api_tokens.json`, 形如 `{"tokens": [...]}`, 原子写 + 尽力 `chmod 0600`。**哈希是无盐 SHA-256 十六进制** (`_hash`, `api_tokens.py:69-70`)。明文 = `"tsp_"` + `secrets.token_hex(32)` = 256 bit 熵。记录形状**恰好**是 `{id, name, token_hash, scopes, created_at, last_used_at, revoked}` (`api_tokens.py:94-102`) → **无任何过期/TTL 字段**; `revoked` 为布尔标记, 无删除路径。`id` 是 `tok_` + `secrets.token_hex(4)` (仅 32 bit)。**无 owner / tenant / user 字段** —— 单一全局列表 + 模块级写锁。
- **热路径**: 每个 Bearer 请求都会 `verify_token` → 整文件 `read_text` + `json.loads` + 线性扫描比对 (`api_tokens.py:48-57, 124-138`), 且用 `==` 而非 `compare_digest`。
- **SSE 票据** (`services/event_tickets.py` + `api/events.py`): `POST /api/events/ticket` 用 Bearer 换票, 票据 = `"tse_"` + `secrets.token_hex(16)` (128 bit), TTL 60 秒 (`TTL_S = 60.0`, 用 `time.monotonic()`), **一次性** (`consume` 用 `pop`), 在途上限 1000 张 (超限 → 429)。票据存**进程内 dict** —— 文档字符串明说「单进程 uvicorn 场景, 重启清零无害」。
- **重活并发** (`services/heavy_job_limiter.py`): 模块级 `threading.Condition` + FIFO 队列, **进程全局单例** `shared_heavy_job_limiter = HeavyJobLimiter(capacity=2)` (`heavy_job_limiter.py:140`), 权重 `{"normal":1, "mining":2}`。**不跨进程、不按租户**。唯二的跨进程例外是挖掘的 `MiningProcessLock` (`services/mining_process_lock.py`, 用 `msvcrt.locking`/`fcntl.flock`)。
- **密钥** (`secrets_store.py`): `data/user_data/secrets.json`, **明文 JSON + `0600` 权限, 无加密层**; 字段含 `ai_api_key`、`ai_base_url`、`tickflow_api_key`、`custom_webhook_secret`、`email_smtp_password`。访问密码走 `services/auth.py` (PBKDF2-HMAC-SHA256, 20 万次迭代, 随机盐, 常量时间比较), 会话 32 字节 / 30 天; 该模块文档字符串自陈「**单用户, 自托管场景**」「单密码, 不做多用户」。
- **CORS**: 实际是 `allow_origins=["*"]` + `allow_credentials=False` (`main.py:417-423`)。**计划文档里写的 `OPEN_API_CORS_ORIGINS` 白名单在代码中不存在** —— 全仓库仅 `docs/open-platform-plan.md:91` 提到它, 属文档与实现的偏差。
- **部署形态**: 单进程单容器。`Dockerfile` 的 CMD 为 `uvicorn app.main:app --host 0.0.0.0 --port 3018`, **全仓库没有任何 `--workers`**; `docker-compose.yml` 只有一个 `app` 服务。TLS 假定由外部反代终止 (面板自身是明文 HTTP:3018, 且要求反代正确配 `X-Forwarded-For`)。所有进程内状态 (限流桶、票据、事件总线、重活限流器、会话缓存) 都以「只有一个进程」为前提, 代码里写明了 (如 `services/events.py:4`「只做进程内广播 (自托管单进程 uvicorn 场景); 不引入 Redis/MQ 依赖」)。

### 4.2 多租户 / 订阅 / LLM 相关的既有基础

**【已证实 · 检索为空】**

- 对 `backend/**` 与 `mcp-server/**` 检索 `tenant` / `subscription` / `billing` / `user_id` (忽略大小写): **零命中**。不存在用户表、账户模型、订阅/套餐对象。
- `docs/open-platform-plan.md:16` 明确把多租户列为**非目标**: 「不做多用户/多租户（数据层全局单用户是既定取舍; 以「单用户 + 多 Token + scope」满足外部调用方隔离需求）」。
- 仓库里的 `tiers.yaml` 与 `?tier=a` **不是本项目的套餐概念** —— 那是上游数据商 TickFlow 的订阅档位与 OpenAPI 稳定性视图 (`tiers.yaml:8` 注明「业务代码永远不读这张表」)。
- **LLM 方向只有出站客户端, 没有代理/转售端点**: `services/ai_provider.py` 用 OpenAI 兼容 SDK (依赖 `openai>=1.40`) + `ai_base_url` / `ai_api_key` (全局单一密钥), 即面板是**外部 LLM 的消费者**; 没有任何让客户端经由面板转发模型调用的接口。
- 唯一的「远程客户端」是 MCP 桥, 且默认指向本机 (`http://127.0.0.1:3018`)。

### 4.3 现有 MCP 桥

- `mcp-server/pyproject.toml`: 依赖 `mcp>=2.0` 与 `httpx>=0.27`; 入口点 `tsp-mcp-server = "tsp_mcp_server.server:main"`。**没有 `uv.lock`**, 且环境中未安装 `mcp` —— 因此其 API 形状是依据 v2 SDK 文档核对, **非运行时验证**。
- `server.py:16` 的 `from mcp.server.mcpserver import MCPServer` 对 `mcp>=2.0` 是**正确的**: MCP Python SDK v2 将 `FastMCP` 更名为 `MCPServer` 并移到 `mcp.server.mcpserver` (v1 的 `mcp.server.fastmcp` 在 v2 已不存在)。若环境解析到 `mcp<2` 会 `ModuleNotFoundError`。
- **传输: 仅 stdio**。`server.py:158` 是裸 `mcp.run()` (注释「stdio 传输, 由 MCP 客户端拉起」), 无 host/port、无 HTTP/SSE。SDK v2 的 `run()` 支持 `streamable-http`, 但本代码从未选择它。`smoke_test.py` 也是走 stdio 管道握手。
- **鉴权模型**: 单一全局 `TSP_TOKEN` + `TSP_SCOPES` (`server.py:19-22`); `main()` 在缺 Token 时直接退出。`TSP_SCOPES` **只决定注册哪些工具** (展示层), `_call` 只负责带上 `Authorization: Bearer` 转发 (`server.py:32-54`) —— 面板侧注释写明「声明只是『展示哪些工具』, 真正的权限裁决在面板网关」。设计上是「薄桥」。工具集覆盖 `read:market` / `read:analysis` / `read:ext` / `write:ext` / `run:backtest`, 刻意**不含**模拟盘交易类端点。

### 4.4 作为「远程多租户订阅网关」的地基: 够什么、缺什么

**已经够用的部分**:

| 资产 | 为什么可复用 |
| :--- | :--- |
| Bearer Token 通道 + scope 规则表 | 心智模型与 Codex 插件的 `auth: api_key / headerScheme: bearer` 天然对齐, 本机客户端无需改造 |
| scope 分类学 (`read:market` / `read:analysis` / `read:ext` / `write:ext` / `run:backtest` / `paper:trade`) | 可直接映射为套餐权限位 (套餐 = scope 子集 + 额度) |
| 「薄桥」MCP 设计 | 权限裁决留在服务端, 桥不持有业务逻辑; 换远程后端时桥的形态可原样保留 |
| Tier A/B 契约分层与 `/api/openapi.json?tier=a` | 对外承诺边界已有机器可读视图, 是网关契约化的现成起点 |
| SSE 短期票据 (60s 一次性) | 解决「流式连接带不了 Authorization 头」这一远程场景同样会遇到的问题, 范式可直接沿用 |
| `deploy-password.md` 的部署密码流程 | 本地服务的加锁思路可参考 |

**缺失的部分 (按改造顺序)**:

1. **身份层**。当前只有「一个共享密码」和「无 owner 的 Token 列表」。远程订阅网关需要真正的账户实体: `account_id` 必须写进 Token/会话记录 (`api_tokens.py:94-102` 的记录形状需要扩展), 否则无法回答「这张票属于谁、买了什么套餐」。
2. **订阅/套餐状态与强制执行点**。网关目前唯一的杠杆是**单一全局** `OPEN_API_RATE_LIMIT_PER_MIN` (`api_gateway.py:76-82`)。需要: 套餐对象 (scope 集合 + 额度 + 有效期)、按 Token/账户的额度覆盖、以及**到期即拒**——而现有 Token **没有任何 TTL 字段**, 这是最直接的缺口。
3. **共享状态存储**。限流桶、票据、事件总线、重活限流器**全部是进程内**的 (`api_gateway.py:103-105`、`event_tickets.py:23-24`、`services/events.py:4`、`heavy_job_limiter.py:140`)。只要网关横向扩容到 N 个进程: 实际限流变成 ~N 倍、票据跨进程 401、SSE 事件跨进程丢失、重活并发变成 2×N。要么引入共享存储 (Redis 等, 与现有「不引入 Redis/MQ 依赖」的自陈相反, 属有意的架构转向), 要么**明确设计为单进程 + 粘性路由**。
4. **凭证存储升级**。`secrets.json` / `auth.json` 是**明文 JSON + 0600**, 单用户自托管下可接受; 一旦同机承载多租户凭证 (或面板本地保存用户的网关登录态), 单点文件系统泄漏即全量泄漏。需要 OS keychain / 加密层, 或明确把租户凭证留在网关侧、面板只保存自己的会话。
5. **面板侧的出站客户端**。当前 `ai_provider.py` 是「一个全局 key + 一个 base_url」的单用户模型。作为网关客户端需要: 用户级凭证的获取与刷新 (登录 → 会话/刷新令牌)、按用户覆盖 base_url/key、失败与额度耗尽的降级路径、以及**「本地无 key 也能用」的可用性设计**(面板本地优先, 网关是增值路径而非硬依赖)。
6. **Token 校验热路径与哈希**。每请求整文件读 + 无盐 SHA-256 + 非常量时间比较 (`api_tokens.py:48-57, 69-70, 124-138`)。改成远程/账户化存储后, 这条路径需要缓存与更强的哈希 (例如 HMAC/带盐)。
7. **CORS 与文档一致性**。计划里的 `OPEN_API_CORS_ORIGINS` 白名单**未实现**, 实际是 `["*"]` (`main.py:417-423` vs `open-platform-plan.md:91`)。若网关侧有浏览器客户端, 这个偏差需要先补齐或修正文档。
8. **MCP 桥的远程能力**。现有桥是 stdio-only、单全局 Token、默认本机 base。若插件要直连远程网关 MCP, 桥需要: streamable-http 传输 (`mcp.run()` 的传输选择)、按连接注入令牌、以及不再依赖进程级环境变量。若改为「插件 MCP → 远程网关」而面板不参与, 则面板侧只需新增出站客户端。

---

## 对本项目改造的影响

**两条工作流可以解耦, 且不必触碰面板的「单用户/本地」定位。**

1. **插件打包 (工作流 A)** 是**纯增量**, 不改面板代码。可交付物: 根 `plugin.json` + `.codex-plugin/plugin.json` + `mcp.json` + `skills/` + `.agents/plugins/marketplace.json` + 一个安装脚本。建议直接用 `@plugin-creator` / `$plugin-creator` 脚手架起步 (官方推荐入口), 再用 §3.8 的 `package.py` 校验思路把两种清单钉在一起 —— 但要把 `type` 断言从 `stdio` 改为 `streamable-http`。

2. **关键决策点**: 插件的 MCP 指向**本地面板**还是**远程网关**?
   - 指本地: 沿用现有 stdio 薄桥, 零新风险, 但无法承载「转售 + 订阅」。
   - 指远程: 技术上可行 (§2 已证), 但必须①只使用 `streamable-http` (不要 `sse`); ②用公网 HTTPS; ③按 §2.4 的残余不确定性**同时**提供 `codex mcp add <name> --url ...` 的安装路径, 不要只依赖插件自带 `mcp.json`; ④注意 Codex 云端编排下「本地 MCP 不保证可用」, 这实际上是远程方案的利好。
   - 若两者都要: 声明两个 server (本地面板 + 远程网关), 用命名空间区分, 并注意 §3.5 提到的「插件自带 MCP 共享工具预算」风险 —— 这正是 iPhone-use 改用配置层注册的动机。

3. **订阅门槛有两级可选**. `api_key` + `headerScheme: bearer` 是渐进方案 (把订阅密钥当 Bearer 用, 与现有 `tsp_` 前缀完全同构); 真要「用户登录」则用 `oauth` (DCR 或 CIMD, 配 `codex mcp login`)。注意 `clientSecret` 字段**不存在**, 密钥走连接设置流程。

4. **面板作为网关客户端 (工作流 B) 是真正需要动代码的一侧**, 且缺口集中在 §4.4 的 8 条。最省事的落地顺序: 先做「账户 + 套餐 + Token TTL」的**服务端**模型 (缺口 1、2), 再决定共享存储 (缺口 3, 这一步决定网关是单进程还是可横向扩容), 然后才是面板侧出站客户端 (缺口 5)。面板侧的改动应保持「本地功能不依赖网关」——即网关是增值路径, 断网/无订阅时面板仍可用。

5. **需要先修正的一处文档/实现偏差**: `OPEN_API_CORS_ORIGINS` 在计划里存在、在代码里不存在。无论选哪条路, 都应先让它一致, 否则后续设计会基于错误前提。

6. **应当保持不动的边界**: 面板的「单用户 + 多 Token + scope」是**有意**取舍 (`open-platform-plan.md:16`)。远程多租户应当落在**新的网关服务**里, 而不是把面板改造成多租户 —— 这也与「面板保持单用户/本地」的决策一致。

---

## 未能确认的事实

以下条目**没有**从一手来源确认, 不应作为设计依据:

1. **插件自带 `mcp.json` 中的远程 `streamable-http` 条目, 在 Codex CLI 运行时是否与 `config.toml` 手工注册等价。** 官方文档描述的是包格式与提交路径, 未明确 CLI 运行时对插件自带远程 MCP 的处理。存在反向旁证: iPhone-use 作者在 `register_mcp.py` 注释中断言「Config wins over Plugin in Codex」并主动改走配置层注册, 但这**只是该作者的经验陈述**, 我在官方文档中未找到对应说明。
2. **「agent-plugin 共享工具预算」的确切含义与数值。** 同一注释提到「shared agent-plugin tool budget」, 官方文档未见此概念。若该预算真实存在, 它会影响「插件该带多少工具」的设计。
3. **`mcp.json` 中远程 server 的字段到底是 `headers` 还是 `extensions`。** Agent Plugins schema 写 `headers` (且 `additionalProperties: false`), 而 OpenAI 打包文档叙述为 `type`、`url`、`extensions`。二者不一致, 需以实测为准。
4. **技能命名空间格式 `{plugin_name}:{skill_name}`。** 该说法来自搜索结果, 我在官方打包文档页面上未找到原文。
5. **`codex plugin` 各子命令在具体版本上的完整参数集。** 我据以确认命令存在的是 `openai/codex` PR #21396 与文档转述 (含 `--json` 在较新版本才有的说法); 未逐一核对当前发布版的 `--help` 输出。我也未在本机执行任何 `codex` 命令 (遵守只读约束)。
6. **Codex 对 `sse` 的实际行为。** 传输文档未列 `sse`, schema 里仍有该变体; 是否「静默不支持」还是「报错」未确认。规避方案明确: 只用 `streamable-http`。
7. **`extensions["com.openai"].auth` 是否被 Codex CLI 在运行时强制执行** (还是仅 ChatGPT 侧 / 提交门户使用)。文档把它与「提交门户连接表单不支持这类声明」并置, 暗示它属于包内声明; 未找到 CLI 侧行为的一手说明。
8. **`mcp` Python SDK v2 的实际 API 形状。** `mcp-server` 未安装依赖、无 `uv.lock`, 我未运行 `uv sync`(只读约束); `MCPServer` / `mcp.server.mcpserver` 与 `run()` 的传输参数系依循官方 SDK 文档核对, **非运行时验证**。
9. **`.mcp.json`(兼容层) 的权威语义。** 官方打包文档只明确「不要仅把 `.mcp.json` 改名 —— 便携格式还要声明传输 `type`」, 未给出兼容层 `.mcp.json` 的完整 schema。iPhone-use 让两个文件内容**完全相同**, 这是其选择而非规范要求。
10. **iPhone-use 是否真的在「插件自带 MCP」与「配置层 MCP」两条路径上都工作。** 我读的是源码与文档, 未运行安装流程验证。

---

## 来源

**一手规范与官方文档**

- Agent Plugins 1.0.0 MCP schema: https://agent-plugins.org/schemas/1.0.0/mcp.schema.json
- Agent Plugins 1.0.0 plugin schema: https://agent-plugins.org/schemas/1.0.0/plugin.schema.json
- *Package your plugin* — https://developers.openai.com/plugins/build/plugins
- *Model Context Protocol* (Codex CLI 传输与 `config.toml` 字段) — https://learn.chatgpt.com/docs/extend/mcp?surface=cli
- *Build plugins* — https://learn.chatgpt.com/docs/build-plugins
- `openai/skills` — `skills/.system/plugin-creator/SKILL.md`: https://github.com/openai/skills/blob/main/skills/.system/plugin-creator/SKILL.md
- `openai/skills` — `references/plugin-json-spec.md`: https://github.com/openai/skills/blob/main/skills/.system/plugin-creator/references/plugin-json-spec.md
- `openai/codex` PR #21396 "add plugin marketplace CLI commands": https://github.com/openai/codex/pull/21396

**参考实现 (raw 原文, 抓取于 2026-10-11)**

- `plugin.json`: https://raw.githubusercontent.com/zhongerxin/iPhone-use/main/plugin.json
- `.codex-plugin/plugin.json`: https://raw.githubusercontent.com/zhongerxin/iPhone-use/main/.codex-plugin/plugin.json
- `mcp.json` / `.mcp.json`: https://raw.githubusercontent.com/zhongerxin/iPhone-use/main/mcp.json
- `scripts/install.sh`: https://raw.githubusercontent.com/zhongerxin/iPhone-use/main/scripts/install.sh
- `scripts/register_mcp.py`: https://raw.githubusercontent.com/zhongerxin/iPhone-use/main/scripts/register_mcp.py
- `scripts/package.py`: https://raw.githubusercontent.com/zhongerxin/iPhone-use/main/scripts/package.py
- `.agents/plugins/marketplace.json`: https://raw.githubusercontent.com/zhongerxin/iPhone-use/main/.agents/plugins/marketplace.json
- `README.md`: https://raw.githubusercontent.com/zhongerxin/iPhone-use/main/README.md

**本仓库 (file:line 均在正文标注)**

- `backend/app/services/api_gateway.py`、`backend/app/services/api_tokens.py`、`backend/app/services/event_tickets.py`、`backend/app/services/heavy_job_limiter.py`、`backend/app/services/mining_process_lock.py`、`backend/app/secrets_store.py`、`backend/app/services/auth.py`、`backend/app/main.py`、`backend/app/api/events.py`
- `mcp-server/pyproject.toml`、`mcp-server/src/tsp_mcp_server/server.py`、`mcp-server/smoke_test.py`、`mcp-server/README.md`
- `docs/open-platform-plan.md`、`docs/deployment.md`、`Dockerfile`、`docker-compose.yml`、`tiers.yaml`
