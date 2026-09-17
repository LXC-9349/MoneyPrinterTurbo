# Phase 4 — 配音与字幕

## 交付

### 1. TTS provider 适配位 `app/services/tts_provider.py`
- `synthesize(text, voice_name, voice_rate, voice_file, voice_volume)` 统一入口,
  调用方(任务流水线)不再直接依赖 edge-tts
- `active_provider()` 读 `config.app["tts_provider"]`,切换 provider 是改配置而非改代码
- `SUPPORTED_PROVIDERS = ("edge", "cosyvoice2")`;`cosyvoice2` 为**预留实现**:
  被选中时才抛 NotImplementedError,并附完整接入步骤(配置项、cloned voice id、
  时间戳包装),未选中时对 Edge TTS 路径零影响
- 每个 provider 必须返回带 `cues`/`get_srt()` 的对象;`_NullSubMaker` 供仅出音频的
  provider 使用,让"是否需要 whisper 对齐"保持显式

### 2. 分镜级音频 `app/services/shot_audio.py`
原实现:整段旁白一次性合成 → 字幕只能靠 ASR 或估算切分,分镜边界与画面易错位。

现实现:**逐分镜合成 + 拼接**,累加各段时长直接得到精确镜级边界,无需强制对齐:
- `synthesize_shots()` → (audio_path, total_duration, shot_timings, sub_maker)
- 分镜间插入 350ms 静音(ffmpeg anullsrc 生成),避免句子粘连
- `timings_to_srt()` 按分镜生成 SRT,中文标点优先断行、超长硬折行
- 任一分镜失败即返回 None,调用方回落到单次合成(绝不使流程变差)

### 3. 接入点 `task.py`
- `_try_shot_audio()`:仅在存在结构化脚本且分镜 ≥2 时启用
- `generate_audio()`:优先分镜音频,失败回落单次 TTS;音频时长仍以真实文件测量
- `_shot_timings_from_audio_dir()`:字幕阶段**重新读取** audio_shots 下的分段文件
  重算时间轴(而非依赖内存态),因此 `stop_at=audio` 与续跑路径同样正确
- `generate_subtitle()`:优先用精确分镜时间轴生成字幕(优于 whisper 重对齐:
  无模型下载、无 ASR 漂移),失败才走原 provider 逻辑

### 4. 关于 whisper 强制的结论
实测 edge-tts 的 `SubMaker` 本来就输出**词级**时间戳(非均分),所以原版字幕精度
已可接受。真正的改进点是镜级对齐——分镜音频免费提供了它,因此 faster-whisper
**保持可选、不默认开启**(CPU 上 4 分钟音频对齐耗时数分钟,收益已被覆盖)。

## 端到端验证(任务 10589a05)
- 成片 209.5s,1920×1080,91MB
- audio_shots/ 15 段分镜音频;script.json 落盘 15 条精确 shot_timings
- 第 1 条字幕 00:00:00,000 → 00:00:14,180,与分镜音频实测时长 14.18s **完全一致**
- 末条 195.99 → 208.9,与成片总时长吻合
- 字幕文本取自分镜 narration,与画面同源,不会出现"字幕说到后面、画面还在前面"
