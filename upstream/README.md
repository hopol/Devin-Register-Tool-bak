# Devin-Register-Tool

Devin（app.devin.ai）**注册 → 发码 → 获取账号信息 → 建 API Key → 入库**
全链路工具，**纯 HTTP、无浏览器**。

收信走 CF Temp Email Worker（按收件人索引端点），可直接批量出号。
契约不是猜的：从 `devin.ai.har` 解出、再用真实请求逐条复现过，
每条判据都写在 `docs/architecture.md` 里。

> **判据只有一条：`POST /api/{org}/{user}/api-key` 有没有回 `apk_user_…`。**
> 拿到了 user_id/org_id 但没拿到 key，状态是 `partial/registered`，**不是成功**——
> 否则交付物里会混进"看起来有账号、实际不可用"的行。

## 链路

| 阶段 | 请求 | 判据 | 实测耗时 |
| --- | --- | --- | --- |
| 1. 注册 + 发码 | `POST /api/auth1/email/start` `{email, mode:"signup"}` | 200 + `email_verification_token`，邮箱收到 6 位码 | 1~2s |
| 2. 收码 | 轮询 `/api/inbox?email=` | Subject `Your Devin Login Code`，正文 6 位码 | 2~10s |
| 3. 换会话 | `POST /api/auth1/email/complete` `{email, ev_token, code, mode}` | 200 + `auth1_…` token + `user_id` | <1s |
| 4. 账号信息 | `POST /api/users/post-auth` → `GET /api/users/current-membership` / `/api/organizations/{org}` / `/billing/status` | `org_id` / `account_id` / `plan_slug` | 1~2s |
| 5. 建 Key | `POST /api/{org_id}/{user_id}/api-key` | 200 + `apk_user_…` | <1s |
| 6. 入库 | 本地 JSONL 台账 + `result/` 交付物 | — | — |
| 7. 验收 | `GET https://api.devin.ai/v1/sessions`（真打，只读） | 200 | ~0.5s |
| 8. CLI 会话（可选） | `POST /auth/cli/authorize` → `POST /auth/cli/token` | 200 + `windsurf-session` token | 1~2s |

> `mode:"signup"` 不是可选装饰：少了它会退化成"登录"语义 ——
> 新地址直接 `400 No account found for this email`，**且不发信**。
> 对已存在的地址 `signup` 同样可用（等价于登录）⇒ 补跑不需要另开分支。

## 配置

| 变量 | 必填 | 用途 |
| --- | --- | --- |
| `TEMPMAIL_ADMIN_KEY` | ✅ | 建邮箱 / `/admin/*` |
| `TEMPMAIL_BASE` | ✅ | Worker 根地址（`https://<子域>.workers.dev`） |
| `TEMPMAIL_DOMAIN` | ✅ | 建邮箱用哪个域名（必须是 Worker `domains` 之一） |
| `DEVIN_MAIL_BACKEND` | ⬜ | 目前只支持 `cf`（默认） |
| `DEVIN_CODE_TIMEOUT` | ⬜ | 等码秒数（默认 180） |
| `DEVIN_CODE_POLL_INTERVAL` | ⬜ | 轮询间隔（默认 2s） |
| `DEVIN_START_RETRIES` / `DEVIN_START_BACKOFF` | ⬜ | 发码 429 的重试次数 / 退避基数 |
| `DEVIN_START_RATE` | ⬜ | 发码节流（个/分钟，AIMD；默认 8，`0`=关闭） |
| `DEVIN_PACER_MIN` / `DEVIN_PACER_MAX` | ⬜ | 节流速率下限 / 上限（默认 3 / 30） |
| `DEVIN_PROXY_FILE` / `DEVIN_PROXIES` | ⬜ | 代理池文件（默认 `.proxies`）/ 逗号分隔代理 |

> **Remail 后端暂不可用**：Remail 的 `GET /v1/open/projects` 里没有 Devin 项目，
> 而 `POST /v1/open/orders` 必须带 `projectId` ⇒ 拿不到项目号就下不了单。
> `--doctor` 会显式报出来，不会跑到发码阶段才失败。

## 安装

Python 3.11+，唯一运行时依赖 `requests`：

```bash
pip install -r requirements.txt
```

## 快速开始

```bash
PY=python                              # 按本机解释器调整；下同

cp .env.example .env                   # 填必填项
$PY tools/run_e2e.py --doctor          # 环境体检（只读，不发信）
$PY tools/selftest.py                  # 离线自测 88 项
$PY tools/run_e2e.py --count 1         # ★ 全链路：建邮箱 → 发码 → 收码 → 会话 → key
$PY tools/run_e2e.py --count 5 --concurrency 4          # 默认已开启发码节流
$PY tools/run_e2e.py --count 5 --concurrency 4 --start-rate 0  # 关闭节流（429 长尾严重）
$PY tools/run_e2e.py --count 1 --cli-token      # ★ 额外换一条 CLI 会话
$PY tools/cli_login.py --email a@b.com          # ★ 给已有账号换 CLI 会话
$PY tools/run_e2e.py --mode resume --limit 10   # 补跑台账里没出 key 的地址
$PY tools/run_e2e.py --mode scan --limit 30     # 诊断：窗口内邮件 + 漏网主题
$PY tools/batch_report.py               # ★ 批次速率：服务耗时/排队/吞吐/分阶段
$PY tools/verify_keys.py               # ★ 验收：真打 /v1/sessions + 导出 4 份交付物
$PY tools/check_deliverables.py        # ★ 独立复核（只读；退出码 0/1，可当门禁）
```

并发对整条链路有效：每个 worker 一个独立 `requests.Session` +
独立收件箱索引端点，只共享带锁的台账。
⚠️ 并发提高的是"同时处理多少个**不同**地址"，不是"同一地址能发多快"——
同一地址重发验证码会被站点 429（per-email 冷却）。

## 产物

```text
exports/ledger.jsonl     全量台账（含失败），按 email 去重、等级合并
result/success.jsonl     成功账号（账号级）
result/keys.txt          email----api_key----user_id----org_id
result/apikeys.txt       纯 api_key，一行一个
result/accounts.csv      账号信息明细（Excel 可直接打开）
result/summary.json      验收统计 + 每把 key 的验证结果
result/cli_sessions.jsonl CLI 会话（cli_login.py 追加；--cli-token 只写台账字段）
```

台账里每个账号还带四个可用性字段：`billing_error` / `overage_credits` /
`swe2_promo_eligible` / `swe2_priority_eligible`。
`billing_error` 非空（如 `out_of_quota`）时起会话会 403 `billing_restricted`，
与选哪个模型无关 —— 不用登录就能判断"这号能不能跑"。

⚠️ `result/` 里是**明文 API Key**，已在 `.gitignore` 里显式列目录
（`*.txt` 没有任何通配规则覆盖，只靠 `*.json` 挡不住 `keys.txt`）。

## CLI 会话（可选）

`--cli-token` 会在出 key 之后再走一段 PKCE 授权，拿到 `windsurf-session` token：

```http
POST https://api.devin.ai/auth/cli/authorize   Bearer auth1_… + x-cog-org-id: org-…
{"code_challenge": "<S256>", "code_challenge_method": "S256"}   → {"code": "…"}
POST https://api.devin.ai/auth/cli/token       {"code","code_verifier"}
                                                              → {"token":"eyJ…","webapp_host":"app.devin.ai"}
```

- 授权绑的是**组织**：缺 `x-cog-org-id` → `401 No organizations found for auth1 user`
- `code` **一次性、5 分钟**
- 这个 token **不是** `apk_user_…`：`app.devin.ai/api/*`（401）和
  `api.devin.ai/v1/*`（403）都不认它，只能交给 CLI 用
- CLI 失败**不影响 key**（账号仍记 `keyed`），单独记 `cli_error` 与 `cli_ok` 计数；
  要"必须成功"请用 `tools/cli_login.py`

## 代理池

把代理写进 `.proxies`（一行一条，已在 `.gitignore`），或设 `DEVIN_PROXIES`：

```text
http://user:pass@host:10000
http://user:pass@host:10001
```

- 支持两种凭据形态：`http://user:pass@host:port`（HTTP 代理）和
  `https://<随机串>.provider.com:port`（HTTPS 代理，凭据在子域里）；
  后者日志里会把子域打码成 `<token>….proxy.example…`，不会把 token 打出来；
- 每个账号**排他租一条出口**（同一条不会同时跑两个账号），全链路
  （发码/换会话/账号信息/建 key/CLI）都走同一 IP；
- 每条代理绑一个 `requests.Session` 跨账号复用（TLS 会话），租出前清 cookie；
- 连续 3 次网络失败自动摘除；凭据在日志/台账里一律脱敏；
- `--no-proxy` 直连；`--proxy-file` 指定别的文件。

## 效率（实测）

| 批次 | 出口 | 并发 | 节流 | 429 | 服务耗时 mean | 墙钟 | 吞吐 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 50 号 | 直连 | 5 | 关 | 19 | 15.26s* | 301s | 9.96/min |
| 15 号 | 直连 | 4 | 8/min | 0 | 12.87s | 109s | 8.27/min |
| 50 号 | **10 代理** | 10 | 8×10 | 0 | 13.4s | **82.5s** | **36.35/min** |
| 50 号 | **21 代理**（10 HTTP + 11 HTTPS） | 16 | 8×21 | **0** | **13.24s** | **53.6s** | **56.02/min** |

\* 只统计未被 429 的账号。含 429 时第一批均值 29.46s、p95 88s。

结论：**吞吐随出口 IP 数近似线性放大**（10→36/min、21→56/min），延迟基本不变；
再往上只能加代理/提高并发，或压缩单号服务耗时（account 阶段 5.7s 是下一步）。
四项优化（邮箱预建 / CLI 并行 / 首轮快探 / Session 复用）合计把
mailbox 降到 ~0、CLI 3.4s→0.9s。

## 三个必须记住的坑

1. **`x-cog-org-id` 不能省。** `complete` 之后不调 `post-auth`，
   `GET /api/users/info` 会 401 `No organizations found for auth1 user`；
   调了 `post-auth` 但不带 `x-cog-org-id`，同样 401。
2. **`org_id` 已含 `org-` 前缀。** 路径自己再拼一个 `org-` 会得到
   404 `Organization not found`——这个 404 极易被误读成"接口下线"。
3. **API Key 只在创建那一刻返回。** 已存在时 `exists=true` 但取不回旧 key ⇒
   只能标 `partial/失败`，不能"重建一把"，也不能假装成功。

## 安全与脱敏

以下文件**永不进仓库**（`.gitignore` 已覆盖）：

| 文件 | 为什么 |
| --- | --- |
| `.env` | 临时邮箱 Worker 的 admin key |
| `.proxies` | 代理账号/密码（或子域 token） |
| `result/` | 明文 API Key、CLI 会话 token |
| `exports/` | 台账、日志、批次副本 |
| `*.har` / `*.eml` | 抓包与会话，含 auth1 token |

提交前自检（应输出空）：

```bash
git ls-files | grep -E '\.env$|\.proxies|^result/|^exports/|\.har$'
```

> `result/keys.txt` 是明文凭据，按交付物管理；`--cli-token` 的会话 token
> 能冒充账号，泄露后需在 Web UI 吊销。代理凭据在日志里一律打码。

## 目录

```text
src/config.py      配置 + .env 加载 + 校验
src/tempemail.py   CF Worker 客户端（建箱 / 按收件人取信 / 轮询）
src/mailrules.py   收件规则表 + 6 位码提取（含 VERP 8 位数字陷阱）
src/devin.py       Devin 站点客户端（start/complete/post-auth/info/api-key）
src/stages.py      单账号流水线（状态机 + 遇错分流）
src/runner.py      批量 / 并发 / 汇总
src/ledger.py      台账（并集合并、失败重跑不抹凭据）
tools/             入口脚本（run_e2e / cli_login / verify / batch_report）
tools/tests/        离线单测（88 项，不碰网络）
docs/architecture.md  站点契约真源（HAR 反推 + 实测记录）
requirements.txt   运行时依赖（仅 requests）
docs/architecture.md  HAR 解出的完整契约与实测记录
```
