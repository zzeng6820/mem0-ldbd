# Mem0 AML Adapter

将 **Mem0 官方托管服务**封装为 AML 评测接受的 Add / Search HTTP 接口。适配层负责请求格式转换、鉴权、用户隔离、写入状态跟踪和幂等回执；记忆提取与检索由 Mem0 官方 API 提供。

本仓库公开的是接口适配代码，不包含 Mem0 托管后端、模型或实现。支持文本与代码输入；图文输入采用**保留文本、忽略图片**的兼容方式，不提供图像理解能力。

## 功能概览

- 提供 AML 格式的 `POST /add`、`POST /search`，以及健康检查接口。
- 使用独立的访问密钥保护适配层，Mem0 官方密钥仅保存在部署环境。
- 按 `user_id` 隔离检索范围，跨该用户的会话检索。
- 使用 SQLite 保存请求状态和成功回执，支持重复请求去重、重启恢复和异步事件对账。
- 对长会话按固定规则分段，保留文本顺序、角色和毫秒时间戳。
- 接受有序图文内容数组；忽略图片，明确返回兼容处理说明。

默认 `legacy` 模式使用 Mem0 V1 Add 和 V2 Search，也可通过配置选择 `v3` 模式。适配层版本与 Mem0 API 版本分别管理。

## 快速开始

使用 Python 3.12，在仓库根目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，填写以下两项：

- `MEM0_API_KEY`：Mem0 官方 API Key。
- `WRAPPER_API_KEY`：评测客户端访问本服务时使用的独立密钥。

可用下面的命令生成访问密钥，将结果保存到 `.env`：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

启动服务：

```bash
.venv/bin/python scripts/run.py
```

默认监听 `127.0.0.1:8894`。供远程评测使用时，按部署环境设置 `HOST`、`PORT` 和网络访问规则。已有 `.env` 时无需再次复制示例文件。

### 主要配置

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `MEM0_BASE_URL` | `https://api.mem0.ai` | Mem0 官方服务地址，要求 HTTPS |
| `MEM0_API_MODE` | `legacy` | `legacy` 或 `v3` |
| `HOST` | `127.0.0.1` | 监听地址 |
| `PORT` | `8894` | 监听端口 |
| `REQUEST_DB_PATH` | `data/requests.sqlite3` | 持久化请求账本 |
| `UPSTREAM_TIMEOUT_SECONDS` | `60` | 单次上游请求超时，单位秒 |
| `ADD_WAIT_SECONDS` | `600` | 异步事件等待时长，单位秒 |
| `POLL_INTERVAL_SECONDS` | `1` | 前台事件轮询间隔，单位秒 |
| `MAX_ADD_CONCURRENCY` | `64` | Add 并发上限 |
| `MAX_SEARCH_CONCURRENCY` | `64` | Search 并发上限 |
| `RECONCILE_INTERVAL_SECONDS` | `30` | 后台每轮对账结束后的等待时间 |
| `RECONCILE_BATCH_SIZE` | `100` | 每轮最多检查的请求数 |

并发上限不代表 Mem0 账号的实际吞吐额度，应结合账号限制和评测要求调整。长会话包含多次上游写入，端到端耗时可能超过单次上游请求的超时设置。

### 使用 systemd

仓库提供通用用户级模板。将模板中的 `/absolute/path/to/ldbd_mem0` 替换为实际仓库目录后使用：

```bash
mkdir -p ~/.config/systemd/user
cp deploy/ldbd-mem0.service.example ~/.config/systemd/user/ldbd-mem0.service
# 编辑服务文件中的 WorkingDirectory 和 ExecStart
systemctl --user daemon-reload
systemctl --user enable --now ldbd-mem0.service
systemctl --user status ldbd-mem0.service
```

修改 `.env` 后执行 `systemctl --user restart ldbd-mem0.service`。注销后能否继续运行取决于服务器的用户服务配置。

## API

Add 与 Search 均使用请求头：

```http
Content-Type: application/json
X-Api-Key: <WRAPPER_API_KEY>
```

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/add` | 写入文本记忆并返回处理回执 |
| `POST` | `/search` | 检索文本记忆证据 |
| `GET` | `/live` | 检查进程存活状态 |
| `GET` | `/health` | 检查两个密钥是否已配置 |

兼容别名 `/v1/memories/add`、`/v1/memories/search` 与主接口语义相同。健康检查无需鉴权；`/health` 在两个密钥均已配置时返回 200，否则返回 503。它不调用 Mem0，不能用来确认密钥有效性或上游连通性。

### Add

请求示例：

```json
{
  "request_id": "example-request-1",
  "user_id": "example-user-1",
  "session_id": "example-session-1",
  "messages": [
    {"role": "user", "content": "I prefer tea.", "timestamp": 1704067200000}
  ]
}
```

文本写入在 Mem0 返回完成结果或异步事件确认成功后，返回 HTTP 200：

```json
{
  "success": true,
  "request_id": "example-request-1",
  "user_id": "example-user-1",
  "session_id": "example-session-1"
}
```

`role` 支持 `user` 和 `assistant`。`session_id` 映射为 Mem0 的 `run_id`。可选 `timestamp` 使用 Unix 毫秒，按消息顺序保存到上游 `metadata.ldbd_message_timestamps`。字符串文本保持原样。`async_mode` 可省略；兼容值为 `false`，不支持 `true`。

### Search

```json
{
  "query": "What drink do I prefer?",
  "options": ["A. Tea", "B. Coffee"],
  "user_id": "example-user-1",
  "top_k": 100
}
```

响应示例：

```json
{
  "success": true,
  "data": [
    {"id": "memory-id", "content": "User prefers tea.", "score": 0.8}
  ]
}
```

- `top_k` 必填，支持 1 到 1000；实际提交值按评测配置填写。
- `options` 是可选的选择题选项数组，开放题可省略。服务仍使用原问题检索，不生成最终答案。
- 可通过 `search_options.threshold`（0 到 1）和 `search_options.rerank` 调整检索。
- 检索强制限定相同的 `user_id`，不按 `session_id` 过滤。不同评测若需要隔离，应使用不同的 `user_id`；Add/Search 均拒绝带 `*` 的用户 ID。
- 结果去重后限制为最多 `top_k` 条。未知上游响应格式会返回错误。

### 图文兼容处理

`messages[].content` 与 Search 的 `query` 均支持非空字符串或有序内容数组：

```json
[
  {"type": "text", "text": "Original text before the image."},
  {"type": "image_url", "image_url": {"url": "data:image/png;base64,<VALID_BASE64>"}},
  {"type": "text", "text": "Original text after the image."}
]
```

示例中的 Base64 是格式占位符，实际请求应使用有效编码。

文本部分按原顺序用换行连接，图片部分被忽略。混合 Add 中没有文本的消息会被跳过；其余消息保留角色、顺序和时间戳。含图片的成功响应附带 `ignored_image_parts` 和 `warnings`。

纯图片 Add 返回 HTTP 200、`success: true`、三个请求 ID 和 `no_op: true`，仅保存幂等回执，不调用 Mem0。纯图片 Search 返回 HTTP 200、`success: true` 和 `data: []`。图片内容不参与记忆或检索，相关评测结果应按文本兼容能力理解。

图片仍须符合输入格式：仅接受内联 Base64 JPEG、PNG、WebP，拒绝远程 URL；单图解码后最多 10 MiB，单次 Add 图片总计最多 30 MiB。服务校验编码和大小，不执行图像识别，也不向上游发送或持久化原图。错误响应不回显原始正文或 Base64。

## 幂等与故障恢复

请求账本保存 ID、输入摘要、事件 ID、状态和成功回执，不保存聊天正文或密钥。部署迁移和重启时应保留账本。

- 同一 `(user_id, request_id)` 与相同输入复用已完成回执，并发重复请求等待同一个任务；相同标识与不同输入返回 409。摘要涵盖原始图文输入，忽略图片不改变幂等校验规则。
- 异步事件 ID 会先持久化再轮询。等待超时返回 504；已有事件的后续请求继续查询状态，不重新写入。
- 写入超时、网络中断或部分 5xx 可能意味着上游已接受请求，此时保留不确定状态，不盲目重发。明确 429 拒绝可在后续请求中重试。
- 后台仅查询已保存的事件；全部事件确认成功后更新本地回执。它不会重新 POST 记忆，也不会将未知或失败状态报告为成功。
- 对账跳过正在处理的请求，并轮转检查；探测并发为 4，单个事件最多等待 10 秒，整批最多等待 30 秒，每条请求每轮最多检查 4 个事件。
- 已完成状态不会被迟到的失败结果覆盖。没有事件 ID 的不确定写入需先核查上游记录，不能通过清空账本或更换请求 ID 重放。

长会话采用固定 `partition-v1` 计划，按顺序拆为每次最多 20 条消息、20,000 字符的上游请求。超长正文按字符拆分，保留全部文本、角色和时间戳；无法满足非空消息约束时显式报错。

内部子请求使用确定性的 `ldbd-part:` ID，分别保存处理状态，全部完成后才返回父请求成功。重试复用完成部分，继续尚未提交的部分或查询已有事件；不重发不确定部分。后台只确认已有子请求的完成状态，恢复尚未提交部分需要客户端提供原始请求。统计外部请求时应排除 `ldbd-part:` 前缀。

已经向上游提交的旧请求不会自动改为分段重发。修复历史失败前，应先核对原始输入、事件和上游存储结果，避免重复写入。本服务不承诺跨网络和数据库故障的严格 exactly-once。

可手动执行一次事件对账：

```bash
.venv/bin/python scripts/reconcile_events.py
# 限定已有请求，可重复传入该参数
.venv/bin/python scripts/reconcile_events.py --request-id <existing-request-id>
```

仅运行单个 Uvicorn worker，不要让多个实例共享同一账本。更换 Mem0 账号或项目时，应使用独立的 `REQUEST_DB_PATH`；旧项目的成功回执不代表新项目已有相应数据。

## 测试与 AML 提交

安装测试依赖并验证：

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q app scripts
.venv/bin/python scripts/check_startup.py
```

测试通过模拟上游覆盖接口契约、鉴权、用户隔离、图文兼容、幂等、分段写入、异步事件和故障恢复，不调用真实 Mem0 API。接入评测前，使用独立测试用户完成真实 Add / Search 联调，再运行平台 Smoke。提交字段与能力说明见 [SUBMISSION.md](SUBMISSION.md)。

## 公开发布

- 仅提交密钥为空的 `.env.example`，真实密钥保存在各部署环境的 `.env`。
- `.gitignore` 排除运行数据库、日志、虚拟环境、私有说明和机器专属配置。分享部署方式时使用通用模板。
- 发布前运行 `python3 scripts/check_secrets.py --all`；暂存后再次运行 `python3 scripts/check_secrets.py` 检查实际提交内容。
- 检查 Git 历史和提交作者信息。`.gitignore` 不会移除已提交内容，也不会隐藏历史中的个人信息。
- 分享源码文件时不要包含 `.git`、`.env`、数据库、日志或私有备份。仓库地址和凭证配置不应含明文 Token。

## 参考资料

- [AML 使用文档](https://agentmemories.ai/docs)
- [AML API Guide](https://agentmemories.ai/api-guide)
- [Mem0 开源项目](https://github.com/mem0ai/mem0)
- [Mem0 Add API](https://docs.mem0.ai/api-reference/memory/add-memories)
- [Mem0 异步事件查询](https://docs.mem0.ai/api-reference/events/get-event)
