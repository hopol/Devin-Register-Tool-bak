# 架构与站点契约（从 HAR 解出 → 真实复现）

本文是**唯一**的契约真源。所有结论都标注了来源：

- `HAR` = `devin.ai.har`（723 条 entry，仅 `app.devin.ai`）
- `实测` = 2026-10-31 用真实地址复现的结果

## 0. HAR 里能直接看到什么

| 现象 | 结论 |
| --- | --- |
| 723 条 entry 里**没有** `Cookie` / `Authorization` 请求头 | HAR 被脱敏过（`postData` 也被剔除，`bodySize=0`） |
| `POST /api/auth1/email/start` 的 `content-length: 50` | 反推 body = `{"email":"<22 字符地址>","mode":"signup"}` |
| `POST /api/auth1/email/complete` 的 `content-length: 368` | 反推 body 含 email + `email_verification_token` + 6 位 `code` + `mode` |
| 前端 chunk 里有 `auth1_session`（localStorage）、`Authorization: Bearer ${token}` | 会话不是 Cookie，是 Bearer token |
| `GET /api/users/info` 请求头带 `x-cog-org-id` | 组织作用域必须显式传 org |

> 脱敏 HAR 仍然能定契约：**请求形状靠 content-length + 前端 chunk 反推，
> 再用真实请求验证**。不验证就写代码，等于把猜测当事实。

## 1. 登录方式探查（可选，但决定分叉）

```text
POST /api/auth1/connections   {"product":"devin","email":"<addr>"}
→ 200 {"connections":[email, openai, google, github…],
       "auth_method":{"method":"not_found","has_password":false,…}}
```

新地址 `method=not_found` ⇒ 走邮箱验证码。这是"注册还是登录"的**唯一**判别位，
但两种意图共用同一套 `start/complete`，所以本工具不必分叉。

## 2. 发码

```text
POST /api/auth1/email/start
Content-Type: application/json
{"email":"<addr>","mode":"signup"}
→ 200 {"ok":true,"message":"Code sent.",
       "email_verification_token":"<jwt>"}   # JWT，267 字符
```

实测分叉：

| 输入 | 结果 |
| --- | --- |
| `{"email"}`（无 mode） | `400 No account found for this email`，不发信 |
| `{"email","mode":"login"}` 新地址 | 同上（登录语义要求账号已存在） |
| `{"email","mode":"signup"}` 新地址 | `200 Code sent.` |
| 同一地址重复 start | `429 Rate limit exceeded`（**per-email**：换新地址立刻 200） |
| 已有账号 + `mode:signup` | `200 Code sent.`（等价登录）⇒ 补跑可用 |

`email_verification_token` **必须与本轮的码配对**。补跑路径里重发码后，
旧 token 配新码会得到 `401 Invalid or expired code`——看起来像"码过期"，
其实是状态没更新。

## 3. 邮件

```text
From:    bounces+44927189-54bb-<local>=<domain>@em3779.cognition.ai
Subject: Your Devin Login Code
Body:    Verify your email for Devin - 517536
         …enter the verification code below…
         517536
         This code expires in 10 minutes.
```

抓码规则见 `src/mailrules.py`：

- 发件人域 `cognition.ai` + 主题含 `devin` 且含 `code|verif`
- 码优先级：主题后缀 → 正文锚点（`verification code below`）→ 全文最高频 6 位整词
- 🔴 VERP 地址里有 8 位数字（`44927189`），裸 `\d{6}` 会切出 `449271`。
  边界断言 `(?<![\w-])(\d{6})(?![\w-])` 是必需的。

## 4. 换会话

```text
POST /api/auth1/email/complete
{"email":"<addr>","email_verification_token":"<ev>","code":"517536","mode":"signup"}
→ 200 {"token":"auth1_c5iwfoy…","user_id":"user-cfe012…",
       "email":"<addr>","status":"authenticated","enterprise_redirects":null}
```

- **响应不 Set-Cookie**（实测）。
- `token` = 后续全部请求的 `Authorization: Bearer`。
- 码一次性：错误/过期 → `401 Invalid or expired code`。

## 5. 账号信息

```text
POST /api/users/post-auth                     {"}  + Bearer
→ 200 {"org_id":"org-f6c591…","org_name":"oai-…",…}
```

不调这一步，下一步一定 401 `No organizations found for auth1 user`。

| 接口 | 额外头 | 拿到的关键字段 |
| --- | --- | --- |
| `GET /api/users/info` | `x-cog-org-id` | `user_id` / `created_at` / `product_onboarding_state` |
| `GET /api/users/current-membership` | `x-cog-org-id` | `org.*`（`account_id`/`name`/`onboarding_status`）+ `user.email` |
| `GET /api/organizations/{org}` | `x-cog-org-id` | `account_id` / `created_at` / `onboarding_status` |
| `GET /api/{org}/billing/status` | `x-cog-org-id` | `plan_slug` / `available_credits` / `overage_credits` / `billing_error` |
| `GET /api/toggleable-agent-versions` | `x-cog-org-id` | `toggleable_versions` / `swe2_promo_eligible` / `swe2_priority_eligible` |
| `GET /api/users/onboarding-state` | — | 引导页状态 |

### 可用性判据（2026-10-31 排查 SWE-2）

| 字段 | 含义 | 判据 |
| --- | --- | --- |
| `billing_error` | 组织账单状态 | 非空（如 `out_of_quota`）⇒ `POST /api/sessions` 直接 **403 `billing_restricted`**，**与模型无关** |
| `overage_credits` | 试用/连接奖励到账的额度 | 0 ⇒ 起不了会话；HAR 里可用的账号是 `9.93` |
| `swe2_promo_eligible` | SWE-2 促销资格 | 只影响 75% off 促销/角标，**不代表模型不存在** |
| `swe2_priority_eligible` | SWE-2 Priority 资格 | 影响 `.priority-*` 模式 |

这四个字段已进 `AccountRecord` 台账，出号后不用登录就能判断"能不能起会话 / 能不能用 SWE-2"。

⚠️ `org_id` 本身已含 `org-` 前缀。路径写成 `/api/org-{org_id}/…` 会得到
`404 Organization not found`。这类 404 与"接口下线"长得一样，必须靠路径断言区分。

### onboarding（可选，默认做）

```text
POST /api/organizations/{org}/onboarding/complete   {}
→ 200 {"status":"success"}
```

空 POST，一次生效（`onboarding_status: in_progress → completed`）。
不做也能用 API Key（已实测），但 Web UI 会卡在向导 ⇒ 默认做。

## 6. 建 Key

```text
GET  /api/{org_id}/{user_id}/api-key/exists   → 200 {"exists":false}
POST /api/{org_id}/{user_id}/api-key          （空 body，content-length: 0）
→ 200 {"api_key":"apk_user_…"}
```

- 路径里的 `{user_id}` 是 `user-…`，`{org_id}` 是 `org-…`，**都不要再拼前缀**。
- `exists=true` 时**取不回明文 key** ⇒ 只能判 `partial/失败`，不能重建、
  也不能假装成功。

## 7. 验收

```text
GET https://api.devin.ai/v1/sessions?limit=1
Authorization: Bearer apk_user_…
→ 200 {"sessions":[]}
```

只读、不产生会话、不消耗额度 —— 作为"key 真能用"的判据成本最低。
（`/v1/user`、`/v1/self`、`/v1/organizations` 都是 404，别拿它们探活。）

## 8. 状态机

```text
mailbox → code_sent → registered → keyed
   │         │            │          │
   └─────────┴────────────┴──────────┴──→ failed（附 stage + error）
```

等级（`src/ledger.py:RANK`）：`keyed 6 > registered 5 > code_sent 4 >
mailbox 3 > partial 2 > failed 0`。

🔴 合并规则是"等级高的一方为主，另一方补齐空字段"，且新记录的
`error/last_attempt/last_stage` 永远保留。这样**重跑失败不会抹掉已有的 api_key**——
否则一次网络抖动就会把交付物里的行清空（历史项目踩过这个坑）。

## 9. CLI 授权（PKCE）——把网页会话换成 CLI 会话

入口 URL 形如：

```text
https://app.devin.ai/auth/cli/continue?redirect_uri=http%3A%2F%2F127.0.0.1%3A40473%2Fcallback
  &state=…&prompt=select_account&code_challenge=…&code_challenge_method=S256&cli_pkce_marker=1
```

页面 schema：`redirect_uri? · prompt(none|select_account) · state · connection? ·
login_hint? · code_challenge? · code_challenge_method?`。
`cli_pkce_marker` 不在 schema 里 —— 它是给登录桥（`windsurf.com/auth/devin/bridge`）看的。

### 9.1 真正的两段接口（实测）

```http
POST https://api.devin.ai/auth/cli/authorize
Authorization: Bearer auth1_…      ← 网页会话
x-cog-org-id: org-…                ← 🔴 必需
{"code_challenge":"<S256>","code_challenge_method":"S256"}
→ 200 {"code":"ASu1Q53wmRFy9nQtPfDaqmrm5_i_yZNi6Ly4emxG7a4"}   # 43 字符
```

| 缺什么 | 实测结果 |
| --- | --- |
| `x-cog-org-id` | `401 No organizations found for auth1 user` |
| 会话 | `401` 未认证 |
| PKCE 参数 | 前端不会发起（schema 里是 optional，但 CLI 流程必带） |

```text
```http
POST https://api.devin.ai/auth/cli/token
{"code":"<code>","code_verifier":"<verifier>"}
→ 200 {"token":"eyJ…","webapp_host":"app.devin.ai"}
```

- token 是 JWT，payload 只有 `{"session_id":"windsurf-session-…"}`
- `code` **一次性、5 分钟**：复用 → `401 Invalid or expired code… already been used`
- ⚠️ 这个 token **不是** `apk_user_…`：`app.devin.ai/api/*` → 401、
  `api.devin.ai/v1/*` → 403；它是 CLI/Windsurf 侧凭据（`server.codeium.com` 的 RPC 可达，
  但需要专用 body ⇒ 本工具只做结构自检，不做服务端验证）

### 9.2 账号选择用的字段

`GET https://api.devin.ai/auth/windsurf/eligible-organizations`
（只需 `Bearer auth1_…`，**不需要** org 头）返回：

```json
{"org_id":"org-…","account_id":"account-…","name":"…","display_name":"My Team",
 "enterprise_id":null,"external_org_id":null,"webapp_host":null,
 "is_admin":true,"can_use_cli":true,"can_use_cascade":true,"can_use_ios":false,
 "cascade_deny_reason":null,"is_current":true,"plan_slug":"free",
 "subscription_status":"active","sso_login_required":false,
 "onboarding_status":"completed","has_pending_seat_request":false,"seat_type":null}
```

- CLI 可选判据：`is_admin || can_use_cli`（iOS 看 `can_use_ios`，Cascade 看 `can_use_cascade`）
- 必须匹配当前 `org_id` / `enterprise_id`；不匹配时前端先 `changeOrganization` 再重试
- `prompt=none` 且有可用账号 → 自动授权；`prompt=select_account` → 强制弹选择器
- 若返回 `detail.code=account_terms_required` + `terms_status` ⇒ 先接受条款再重试

### 9.3 redirect_uri 白名单

前端校验函数原文：

```js
http: && hostname === "127.0.0.1" && pathname === "/callback"
```

服务端 `authorize` **不收** `redirect_uri`（回跳由页面完成），所以这是页面侧唯一闸门。
本仓库的 `cli_auth.is_allowed_redirect()` 与它逐条对齐；自动化路径直接拿 `code`
去换 token，不经过浏览器回跳。

### 9.4 实现位置

| 文件 | 作用 |
| --- | --- |
| `src/cli_auth.py` | PKCE / authorize / exchange / 白名单 / JWT 结构自检 |
| `src/stages.py::_stage_cli_session` | 流水线可选阶段（失败不拖垮 key） |
| `tools/cli_login.py` | 独立入口：`--email` 复用账号、`--new` 新注册、`--verify` 离线自检 |
| `result/cli_sessions.jsonl` | `cli_login.py` 的会话凭据落盘（`--no-save` 可关）；流水线的 `--cli-token` 只写台账字段 |

## 10. 效率优化（2026-10-31 50 号压测驱动）

50 号 / 并发 5 的原始数据：墙钟 301s，吞吐 9.96 个/分钟，单号均值 29.46s、
p95 88.12s；其中 **19/50 个全新地址命中 429**，退避把单号拖到 52.6s；
没被 429 的 31 个账号均值 15.26s、p95 18.91s。

### 10.1 三轮对照

| 批次 | 出口 | 并发 | 节流 | 429 | 服务耗时 mean / p95* | 墙钟 | 吞吐 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 50 号 | 直连 | 5 | 关 | 19 | 15.26s / 18.9s | 301s | 9.96/min |
| 20 号 | 直连 | 5 | 20/min（旧算法：减半 / ×1.1） | 5 | 13.28s / 17.4s | 170s | 7.06/min |
| 15 号 | 直连 | 4 | 8/min（新算法：×0.8 / +1） | **0** | **12.87s / 15.5s** | 109s | 8.27/min（收敛 11/min） |
| 50 号 | 10（代理池） | 10 | 8×10（首跑被 30 上限夹住） | 0 | 13.4s / 19.7s | 105.8s | 28.36/min |
| 50 号 | 10（代理池） | 10 | 8×10（修复上限） | **0** | **13.4s / 19.7s** | **82.5s** | **36.35/min** |
| 50 号 | 21（10 HTTP + 11 HTTPS） | 16 | 8×21 | **0** | **13.24s / 21.4s** | **53.6s** | **56.02/min** |

\* 只统计未被 429 的账号。50 号批次含 429 时均值 29.46s、p95 88.12s。

### 10.2 做了哪些改动

| 改动 | 依据 | 实测效果 |
| --- | --- | --- |
| 发码节流（AIMD，起手 8） | 429 是速率超阈值，不是邮件慢 | 0 次 429，p95 88→15.5s |
| 去掉 organization / user_info 单独请求 | membership.org 已含所需字段 | account 阶段 4.94→4.05s |
| 收码轮询 2s → 1s | 2.77s 里一半是轮询粒度 | wait_code 2.65→2.07s |
| `pace_wait` 单独记账 | 排队混进 elapsed 会让"优化"看起来更慢 | 服务/排队两个口径分开 |
| ~~账号信息并行~~（已回退） | 本机出口对同域并发排队：并行 1499ms vs 串行 673ms | 改为串行 2 次请求 |

### 10.3 代理池 + 四项优化（2026-10-31）

10 条 US 出口（每条一个独立 IP），并发 = 池大小 = 10，账号全链路固定同一出口。

| 优化 | 实现 | 实测 |
| --- | --- | --- |
| 代理池 | `src/proxy.py`：排他租约 + 每代理一个 Session + 失败摘除 | 50/50 成功，0 网络失败 |
| 邮箱预建 | `runner.MailboxPrefetcher`（深度 2×并发） | mailbox 1.1s → **~0** |
| CLI 并行 | post-auth 后立刻预热 authorize，与账号信息并行 | cli_session 3.4s → **0.9s** |
| 首轮快探 | 前 3 次 0.3s，之后 1s | wait_code 2.65 → 2.18s |
| Session 复用 | 每代理绑一个 Session，跨账号复用 | 计入整体（含在 13.4s 内） |

🔴 踩到的坑：`StartPacer` 的 `max_per_minute=30` 会把显式传入的 80 **夹回 30**
（第一次 reward 就发生），导致首跑吞吐 28.36/min。修复后同一规模 **36.35/min**。
教训：默认上限不能覆盖调用方显式指定的值。

### 10.4 混合代理池（HTTP + HTTPS，21 条）

后 11 条是 HTTPS 代理，凭据在**最左子域**里 ⇒ `mask()` 必须把它打码
（`<token>….proxy.example…`），否则日志等于明文密码。
`requests` 对 `https://` 代理走 TLS CONNECT，三个域名都实测可达。

- 21 条代理里有 3 条出口是同一个 IP（`71.167.49.67`）⇒ 按 IP 算只有 19 个；
- 因此并发取 **16**（不是 21），给同 IP 的并发留余量；
- 结果：50/50、CLI 50/50、**0 次 429**、0 网络失败、56.02 个/分钟。

### 10.5 三个结论

1. **单 IP 吞吐被站点上限锁死**（~10-11 个/分钟）；**多出口可以线性放大**
   （10 IP → 36.35/min，21 IP → 56.02/min）。单号服务耗时 12.87→13.24s（代理多 ~0.4s），
   靠预建/并行抵消。现在瓶颈回到单号服务耗时（10/13.4×60≈44.8/min 是
   10 条代理的理论上限）。
2. **429 是自适应惩罚**：50 号爆发后，紧接着的 20 号批次阈值明显降低；
   低速率起手（8/min）能完全避开，并靠加性增爬到 11/min。
3. **并发不是越大越好**：并发 4~5 已能填满站点上限；再高只是把请求堆在
   出口排队、放大 429。要提高吞吐只能加出口 IP（或接受当前上限）。

⚠️ 判据：看 **service_seconds（扣排队）** 是否下降，不要看含排队的 e2e 均值。

## 11. 已知限制

| 限制 | 影响 | 处置 |
| --- | --- | --- |
| 发码 per-email 冷却（429） | 同一地址短时间只能发一次 | 退避重试（默认 25/50/75s）；批量用新地址 |
| 验证码 10 分钟有效 | 收信/换会话必须在窗口内 | 收码阶段超时 180s，超时即失败重跑 |
| API Key 只返回一次 | key 已存在则无法交付 | 标失败并提示换地址/先吊销 |
| Remail 无 Devin 项目 | 不能切付费邮箱后端 | `--doctor` 显式报错，保持 cf |
| CF Worker retention 全表 100 行 | 跑批太快时旧邮件被挤掉 | 收信一律走 `?email=` 索引端点，读后即用 |
| CLI code 5 分钟 + 一次性 | 拿到必须立刻换 | `cli_auth.cli_login` 内部连续两步，不落盘中间码 |
| CLI token 无法服务端验证 | 只能结构自检 | 交付时记录 `session_id`，可用性由 CLI 侧确认 |
