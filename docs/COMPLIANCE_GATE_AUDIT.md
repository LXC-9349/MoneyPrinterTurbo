# 合规导出闸门核查报告

## 背景

里程碑 3 #7 引入合规导出闸门：存在阻断级提醒（质量闸门未过 / AI 标识缺失 /
素材授权缺失）且尚未"我已知晓"时，拒绝导出成片。

## 核查范围

| 端点 | 文件 | 是否受闸门保护 |
|------|------|:-:|
| `GET /tasks/{id}/export` | workbench.py:757 | ✅ 是 |
| `POST /tasks/{id}/acknowledge-compliance` | workbench.py:789 | 闸门解锁操作 |
| `GET /tasks/{id}/video` | workbench.py:1104 | ❌ 否（内嵌播放） |
| `GET /stream/{file_path}` | video.py:557 | ❌ 否（通用流） |
| `GET /download/{file_path}` | video.py:609 | ❌ 否（通用下载） |

## 结论

1. **正式导出入口 `/tasks/{id}/export`**：正确受闸门保护（412 响应，附阻断项列表）。
   前端"导出"按钮对应此端点，用户体验链路完整。

2. **内嵌播放 `/tasks/{id}/video`**：不受闸门保护。
   - **设计意图**：任务详情页预览播放器，让用户在确认合规前也能"看一眼"。
   - **风险**：低。预览不触发下载保存动作，且需要 task_id（UUID）才能访问。

3. **通用路径 `/stream/{file_path}`、`/download/{file_path}`**：不受闸门保护。
   - **设计意图**：历史遗留端点，支持任意 storage/tasks 下文件路径访问。
   - **风险**：中。若攻击者获知任务目录 UUID + 最终文件名（如 `final-1.mp4`），
     可直接绕过闸门获取成片文件。

4. **自建徽章**：`routes/persons/[name].tsx` L425-426 已实现。
   当 `profile.source === "custom"` 时渲染 `<Badge tone="primary">自建</Badge>`，
   后端 `person_store.py` 在创建时正确写入 `source="custom"`。功能完备，无需修改。

## 建议

| 优先级 | 建议 | 说明 |
|:--:|------|------|
| P2 | 将 `/stream` `/download` 标记为 deprecated，前端统一走 `/tasks/{id}/export` | 减少绕过面；通用路径端点在 API 文档中移除可见性 |
| P3 | 对 `/stream` `/download` 增加 final 视频检测（路径含 `final*.mp4` 时要求合规确认） | 向后兼容，不影响中间件/字幕/音频文件访问 |
| P4 | 生产部署时 Nginx 层限制 `/stream` `/download` 仅允许 API Key 认证 | 运维侧防护，无需代码变更 |

当前版本（开发/自部署阶段）闸门逻辑集中在 export 端点已满足产品需求；
上述 P2-P4 作为后续加固项，不影响当前里程碑交付。
