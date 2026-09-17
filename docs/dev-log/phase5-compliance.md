# Phase 5 — 合规收尾(AI 标识 + 溯源 sidecar)

## 交付 `app/services/compliance.py`

### 1. AI 生成标识(烧录进画面)
- `build_watermark_clip()` 生成常驻角标,默认右下角、字号 28、不透明度 0.55,
  白字黑描边,确保在任意底色上可读且不遮挡底部居中的字幕
- 位置可选 `bottom_right / bottom_left / top_right / top_left`,全部经配置控制
- 文案默认「内容由AI生成」,英文任务自动用 "AI-generated content",
  可用 `ai_content_label_text` 覆盖
- 叠加时机在**字幕之后**,因此角标永远在最上层,不会被字幕或转场覆盖
- 任何失败(字体缺失/TextClip 异常)只记 warning,视频照常产出(仅少角标)

### 2. 溯源 sidecar `<视频同名>.compliance.json`
内容:
- `ai_generated`:是否 AI 旁白、标识文案、标识是否成功应用
- `models`:本次使用的 LLM provider+model、TTS provider+voice、字幕 provider、编码器
  (从 config 实时读取,不硬编码)
- `script`:分镜数、事实校验统计、**unverified 镜头清单及原因**(供人工复核)
- `sources[]`:每张图片的 provider / asset_id / 公开来源页 / license / license_url /
  creator / credit / visual_type / 本地文件名
- `license_summary`:按协议计数(如 Public domain: 8, CC BY 4.0: 1)
- `notes`:版权说明(书目仅作事实参考不成段引用;unverified 需复核)

素材来源从任务 `script.json` 读回(`_load_material_sources`),而非依赖内存态,
因此续跑/stop_at 场景同样正确。

## 接入点
- `video.py::generate_video`:字幕合成后叠加角标
- `task.py::_run_pipeline`:成片产出后为每个 final 视频写 sidecar
- `config.example.toml`:新增 `ai_content_label_*` 与 `tts_provider` 说明

## 设计取舍
- **烧录而非平台开关**:抖音/B站/YouTube 的"AI 生成"声明按钮可能变动,
  且手动上传时容易漏勾;烧录进画面才是跨平台一致且不可撤销的合规动作。
- **sidecar 与视频同名同级**:人工审查时直接 `list` 视频目录即可拿到配对材料,
  不需要进数据库或重新跑流水线。
