# 常见问题（FAQ）

## `RuntimeError: No ffmpeg exe could be found`
视频合成、静音轨、pydub 转码都依赖 ffmpeg。若无法自动获取，从
[gyan.dev](https://www.gyan.dev/ffmpeg/builds/) 下载后指定路径（Windows 分隔符用 `\\`）：
```toml
[app]
ffmpeg_path = "C:\\Users\\you\\Downloads\\ffmpeg.exe"
```
或设环境变量 `IMAGEIO_FFMPEG_EXE`。后端 `utils.check_ffmpeg_ready()` 会在出片前探测并给出可操作提示。

## 提交任务在音频阶段秒级失败
`voice_mode = tts` 时配音「三件套」需相容：`voice_name` 不能为空、`tts_server` 不能是缺 Key 的付费源。
最省事：用免费的 `azure-tts-v1`（即 Edge TTS，无需 Key）。阶段 2 会把三件套收敛为「选一个音色」。

## 端口连不上 / 连到 8080 没反应
后端默认 **8917**（不是 8080），文档/示例已对齐。前端 dev 在 **7310**。
独立前端浏览器直连后端需在启动时注入 `CORS_ALLOWED_ORIGINS`（开发用 `scripts/dev_serve.py` 自动注入）。

## Whisper 模型下载失败
首次使用会尝试从 Hugging Face 下载 `faster-whisper-large-v3`；网络受限时手动下载放到
`models/whisper-large-v3`，或在设置里改回 `subtitle_provider = "edge"`（Edge TTS 自带词级时间戳）。

## 史料库 / 人物库是必须的吗？
- **人物数据层是核心**（档案、事实、素材、脚本、视频、合规），前端「素材库 → 人物」依赖它。
- **史料全文库（`scripts/library/*` 离线 SQLite）是可选增强**：缺失时脚本 grounding 与
  时代词检索优雅降级，不影响基本出片。构建方式见归档的
  [历史档案素材库使用指南](archive/%E5%8E%86%E5%8F%B2%E6%A1%A3%E6%A1%88%E7%B4%A0%E6%9D%90%E5%BA%93%E4%BD%BF%E7%94%A8%E6%8C%87%E5%8D%97.md)。

## 成片画面不对 / 素材缺失
古代人物通常没有真实照片，`historical_archive` 会取画像/雕塑/文物等公有领域素材；
某分镜无档案素材时降级为同人物其它照片，再降级 b-roll。当前降级较静默，阶段 3 确认屏
会显式列出缺料镜头供你换检索词/上传/AI 生成/选候选。

## 授权白名单为何拒绝 CC BY-SA / NC / ND
仅接受 PD / CC0 / CC BY，避免传染性授权与商用限制带来合规风险。是否放宽（含自动署名方案）
在阶段 4 评估，由维护者决策。

## 我改了 config.toml 但界面没变化
界面写入走 `PUT /api/v1/settings` → `save_config()` 回写 `config.toml`。手改文件后需重启后端；
项目向导里的参数是**项目级 draft**，与全局配置分层，不互相覆盖。
