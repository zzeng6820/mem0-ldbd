# LDBD Mem0 官方平台适配服务（兼容重建版）

重建时间：2026-10-22。旧服务器源码已丢失；本目录依据原接入记录、评测协议和 Mem0 官方 REST 契约重新实现，**不是原源码的逐字恢复**。

## 已恢复的边界

- 原登记系统：`mem0-api v3`，工业榜、`participant_api`。
- 原运行配置：同步 Add、每块 20 条消息、Top K 100、写入及检索并发各 64。分块由评测端负责，服务端不再次分块。
- 原始 Mem0 官方 Key、服务源码、内部算法选项和上游版本没有恢复。原记录的 `v3` 是本地封装版本名，不能据此认定使用了 Mem0 V3 API。

默认用显式 REST `legacy` 模式：V1 Add + V2 Search。可选 `v3` 模式。官方平台算法可能已升级，兼容接口不保证复现旧榜单分数。

## 配置与启动

需要 Python 3.12。先安装下文依赖，再复制 `.env.example` 为 `.env`。填写官方 `MEM0_API_KEY`，并生成独立的 `WRAPPER_API_KEY`（例如 `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`）。两者不同；真实值只保存在本机 `.env`，不要提交或写入日志。

```bash
cd /absolute/path/to/ldbd_mem0
# 初次安装；已有 .env 时跳过复制，避免覆盖
cp .env.example .env
chmod 600 .env
nano .env
.venv/bin/python scripts/run.py
```

默认监听 `127.0.0.1:8894`。需要供远程评测器使用时，将 `.env` 的 `HOST` 改为合适的网卡地址或 `0.0.0.0`，并按服务器网络策略开放访问。服务不会自行改动防火墙或旧榜单绑定。

长期运行可使用通用用户级 systemd 模板，先把模板中的路径改成实际目录：

```bash
mkdir -p ~/.config/systemd/user
cp deploy/ldbd-mem0.service.example ~/.config/systemd/user/ldbd-mem0.service
nano ~/.config/systemd/user/ldbd-mem0.service
systemctl --user daemon-reload
systemctl --user enable --now ldbd-mem0
systemctl --user status ldbd-mem0
# 修改 .env 后：
systemctl --user restart ldbd-mem0
```

注销后用户服务是否继续运行，取决于服务器的 user lingering 配置；本项目不更改该系统设置。

全新机器安装依赖：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
```

如果系统缺少 `ensurepip/python3-venv` 但提供 pip 24，可用不需要 sudo 的方式：

```bash
python3 -m venv --without-pip .venv
python3 -m pip --python .venv/bin/python install -r requirements-dev.txt
```

## API

`GET /live` 返回进程存活状态。`GET /health` 无需鉴权；两个 Key 都配置后返回 200，否则 503。Health 不调用官方 API，因此不能证明 Key 有效或官方网络可达。

Add 与 Search 使用 `X-Api-Key: <WRAPPER_API_KEY>`。兼容别名 `/v1/memories/add`、`/v1/memories/search` 提供相同语义。

`POST /add`：

```json
{
  "request_id": "example-request-1",
  "user_id": "example-user-1",
  "session_id": "example-session-1",
  "messages": [
    {"role": "user", "content": "I prefer tea.", "timestamp": 1704067200000}
  ],
  "async_mode": false
}
```

成功时 HTTP 200，且只在上游同步处理返回已完成结果，或异步事件成功后返回：

```json
{"success": true, "request_id": "example-request-1", "user_id": "example-user-1", "session_id": "example-session-1"}
```

正文及角色保持原样，`session_id` 映射上游 `run_id`；可选时间戳为 Unix **毫秒**，按原顺序保存在上游 `metadata.ldbd_message_timestamps`，不会用写入时间替换。旧服务具体如何处理时间戳已不可考，这一映射是本次重建选择。当前恢复范围为文本记忆，不接收图片/音视频 ContentPart。

`POST /search`：

```json
{"query": "What drink do I prefer?", "user_id": "example-user-1", "top_k": 100}
```

响应：

```json
{"success": true, "data": [{"id": "memory-id", "content": "User prefers tea.", "score": 0.8}]}
```

搜索强制 `filters={"user_id": ...}`，跨该用户的会话检索，不按 session 过滤。只透传可选 `search_options.threshold`（0 到 1）和 `search_options.rerank`；不能从请求注入其他用户的过滤条件。支持官方裸数组、`results`、`memories`、`data` 数组响应，结果去重并限制到 `top_k`。未知响应格式返回错误，不伪装为空结果。

## 幂等与故障恢复

`data/requests.sqlite3` 是持久化请求账本；保存 ID、输入摘要、事件 ID、状态和成功回执，不保存聊天正文或 Key。请与代码一起保留，勿在重启或迁移时删除。

- 同一 `(user_id, request_id)`、相同输入：复用已完成回执；并发重复请求等待同一个任务。
- 同一标识但不同输入：409，`request_id is bound to a different payload`。
- 上游异步返回事件：先持久化事件 ID，再轮询；单次等待默认 600 秒，超时返回 504。后续同一请求继续轮询，不重新写入。
- 写入超时、网络中断或 5xx：结果可能已被上游接受，记为 `uncertain`；后续返回 503，禁止自动重复写入。
- 进程在提交期间退出且未持久化事件/结果：同样视为结果不确定。
- 明确 429 拒绝：后续同一请求允许重试。上游事件明确失败不会报告成功。
- Search 和事件 GET 最多重试三次；Add 不对不确定结果自动重试，也不会自动切换 API 版本重发。

这是在缺少官方幂等提交保证时的保守恢复策略，不保证跨网络/数据库故障的 exactly-once。遇到 `uncertain`，应先由运维按上游记录核对；不要直接清空账本或换 request_id 重放。原评测端存在跳过部分 409 的补丁；本次恢复不会将“正在处理”直接报告为成功。

仅使用单个 Uvicorn worker；不要使用多 worker 或多个实例共享此账本。新旧环境不要复用真实用户 ID 做测试；实际冒烟测试应使用独立的随机 user_id。

重启后需客户端重放相同 `/add` 请求才会继续查询已记录的异步事件。更换 Mem0 账号或项目时，必须使用不同的 `REQUEST_DB_PATH`，旧项目的已完成回执不能代表新项目已写入。为避免 Mem0 的通配符语义突破用户隔离，Add/Search 都拒绝含 `*` 的 user_id。官方鉴权拒绝返回 503，修复 Key 后可重试；上游 409 映射为 502 并标记不确定，不触发旧评测器跳过 409 的行为。

## 验证

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q app scripts
.venv/bin/python scripts/check_startup.py
```

测试使用 `httpx.MockTransport`，覆盖请求契约、鉴权、用户隔离、重复写入、重启恢复、异步成功/失败/超时、不确定写入、429 和 legacy/V3 路由。它们不调用真实官方服务，也不会生成真实记忆或消耗 Mem0 额度。真实 Add/Search 联调需填入官方 Key 后单独完成。

## 依据

官方资料：

- [旧 Python SDK v0.1.118](https://github.com/mem0ai/mem0/blob/v0.1.118/mem0/client/main.py#L119-L156)
- [旧 OpenAPI：V1 Add、V2 Search](https://github.com/mem0ai/mem0/blob/v0.1.118/docs/openapi.json)
- [V1/V2 向后兼容与平台迁移说明](https://docs.mem0.ai/migration/platform-v2-to-v3#backward-compatibility)
- [当前 Add](https://docs.mem0.ai/api-reference/memory/add-memories)
- [异步事件状态](https://docs.mem0.ai/api-reference/events/get-event)

## 公开仓库与密钥

- 提交 `.env.example`，其中密钥项必须保持空值；每个部署环境自行创建 `.env`。
- `.gitignore` 排除 `.env*`（例外 `.env.example`）、虚拟环境、数据库、日志、私有运维说明、机器专属 systemd 单元及恢复归档。使用 `deploy/ldbd-mem0.service.example` 分享部署模板。
- `.gitignore` 不会移除已经暂存或已提交的文件。若文件已被跟踪，应先 `git rm --cached -- <文件>`；真实密钥一旦公开，还需要更换密钥并清理历史。
- 检查发布候选文件：`python3 scripts/check_secrets.py --all`。暂存后运行 `python3 scripts/check_secrets.py`，检查实际 Git index，而非仅检查工作副本。
- 检查器阻止常见凭证格式、与本机 `.env`/origin 凭证相同的值，以及误选的运行文件；它无法识别全部秘密，请结合人工审查及 GitHub Secret scanning / Push protection。
- Git remote URL 不应嵌入 Token；优先使用 SSH 或凭证管理器。`.git/config` 不属于正常提交内容，但不要将其复制进文档、日志或归档。

可选本地 pre-commit hook（不会随 clone 自动安装；已有 hook 时应合并检查步骤）：

```sh
#!/bin/sh
set -eu
cd "$(git rev-parse --show-toplevel)"
exec python3 scripts/check_secrets.py
```

将以上内容保存为 `.git/hooks/pre-commit` 并 `chmod 700 .git/hooks/pre-commit`。不要使用 `git add -f` 强制加入被忽略的配置。
