# AML 提交说明模板

按实际部署填写占位符，不要把 API Key 填进本文件。Memory System Key 应通过平台的专用保密字段提供。

| 字段 | 建议填写 |
|---|---|
| 系统名称 | Mem0 Platform Adapter (text-only) |
| 接口版本 | aml-text-compatible-2026.10.23 |
| 拟测试类型 | 文本、代码；图文测试采用忽略图片的文本降级处理 |
| Add URL | https://YOUR_SERVICE/add |
| Search URL | https://YOUR_SERVICE/search |
| Health URL | https://YOUR_SERVICE/health |
| 鉴权 | X-Api-Key；值取部署环境的 WRAPPER_API_KEY |
| 上游服务 | Mem0 官方托管 API |
| 公开代码 | 填仓库 URL 与本次部署的固定 commit |

组别按平台审核填写。本项目公开的是托管服务的适配代码，不代表 Mem0 托管后端的模型、实现也已开源。原始项目引用：[mem0ai/mem0](https://github.com/mem0ai/mem0)。

## 行为与限制

- 有文字的 Add 保留文本消息的原顺序、角色和毫秒时间戳；等待 Mem0 同步完成或异步事件成功，才回显三个 ID 并返回成功。纯图片 Add 的空操作行为见下文。
- Search 只检索完全相同的 user_id，不以 session_id 限制；Top K 最大为请求值，正式评测使用 100。
- 接受顶层选择题 options，但仍用原问题检索，不生成最终答案。
- **默认文本兼容模式**：图文有序内容数组只保留 text 部分，图片被忽略；响应明确附带忽略数量。纯图片 Add 返回 200 和成功的空操作回执（`no_op: true`），保留幂等性；纯图片 Search 返回 200 和空结果。两者均不调用 Mem0。无视觉模型调用，图片内容不参与答题；此降级处理用于避免有效图片请求报错，不代表具备图像理解能力或已通过平台 Smoke。
- 并发配置是上限，需按 Mem0 账号的实际额度填写评测并发。首次 Smoke 建议低并发验证；不要把默认 64 描述为已验证的官方服务吞吐能力。
- 本地默认单次上游请求超时 60 秒，异步事件等待 600 秒，可通过环境变量调整；反向代理及客户端超时应与实际配置一致。
- 已完成请求回执持久化；不确定写入不自动重放，避免重复记忆。遇到超时应按 README 的恢复策略处理。
- API 数据仅用于评测；不记录正文/图片日志。运行数据及 Mem0 项目内容需按平台要求在评测结束后 30 天内删除；本项目不自动删除上游记忆，运维需执行清理。

## 提交前验证

1. 配置 MEM0_API_KEY 与独立的 WRAPPER_API_KEY；真实凭证不提交 Git。
2. 启动服务，设置正确的对外监听地址/反向代理，并验证 Health 可从评测侧访问。
3. 用独立测试 user_id 完成一次真实文本 Add → Search；模拟测试不能替代官方链路。
4. 在 AML 申请/绑定对应的测试版本，如包含图文测试需声明上述降级行为；获得 Eval Key 后运行平台 Smoke。
5. 只有 Smoke 实际通过后，才填写通过状态并提交 Full。公开代码与部署版本应保持一致。

依据：[使用文档](https://agentmemories.ai/docs)、[API Guide](https://agentmemories.ai/api-guide)。
