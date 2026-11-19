# AML 提交说明

本项目将 Mem0 官方托管服务封装为 AML Add / Search 接口。按实际部署填写下表；真实 API Key 仅通过平台专用保密字段提供。

| 字段 | 填写说明 |
|---|---|
| 系统名称 | Mem0 AML Adapter |
| 接口版本 | 以实际部署代码为准 |
| 支持输入 | 文本、代码；图文输入仅保留文本、忽略图片 |
| Add URL | `https://YOUR_SERVICE/add` |
| Search URL | `https://YOUR_SERVICE/search` |
| Health URL | `https://YOUR_SERVICE/health` |
| 鉴权 | `X-Api-Key`，值为部署环境的 `WRAPPER_API_KEY` |
| 上游服务 | Mem0 官方托管 API |
| 公开代码 | 仓库 URL 与部署 commit |

## 能力说明

- Add 保留文本顺序、角色和毫秒时间戳，等待上游处理完成或异步事件确认成功后返回回执。
- Search 按相同 `user_id` 检索，返回最多 `top_k` 条文本记忆证据；接受选择题选项，不生成最终答案。
- 图文内容数组只保留文本部分。纯图片 Add 返回带 `no_op: true` 的成功空操作回执；纯图片 Search 返回空结果。图片不参与记忆或检索，无视觉模型调用。
- 长会话按固定规则分段写入，子请求分别保存幂等状态，全部完成后才报告成功。
- 已保存事件通过 GET 对账；不确定写入不自动重发。具体超时和恢复规则见 [README.md](README.md)。

本仓库公开接口适配层，不代表 Mem0 官方托管后端已随本项目公开。上游项目：[mem0ai/mem0](https://github.com/mem0ai/mem0)。

## 提交前检查

1. 配置 `MEM0_API_KEY` 和独立的 `WRAPPER_API_KEY`。
2. 设置监听地址及网络访问规则，确认评测端能访问 Health。
3. 使用独立测试用户完成真实文本 Add / Search 联调。
4. 如实声明图片忽略行为，按平台流程运行 Smoke，再提交 Full。
5. 确保公开代码与部署版本一致，清理发布文件及 Git 历史中的凭证和个人信息。

并发值应符合 Mem0 账号额度，不能用本地配置上限代替实际吞吐能力。运行数据和上游记忆由部署者按评测平台要求管理和清理。

参考：[AML 文档](https://agentmemories.ai/docs)、[API Guide](https://agentmemories.ai/api-guide)。
