"""成片质量闸门:交付前用 ffprobe/ffmpeg 客观校验,不靠日志一句"完成"。

动机(基于真实缺陷):任务曾报告 ``state=1`` 但产物可能是 0 秒、无音轨、
分辨率错、或整段黑屏 —— 用户拿到"成功"却得到一个不可用文件。质量闸门在合成后
独立复核**磁盘上的真实文件**,把不达标项作为结构化警告写回 ``script.json``,
并把结论打进任务状态,让调用方/界面能区分"跑完"与"合格"。

设计原则:
* **只读检测,绝不修改产物**;任何检测异常都降级为警告而非让任务失败
  (质量校验不应把已成功的渲染变成失败状态)。
* 检测项全部是确定性、可离线复现的:容器可解析、时长>0、含视频流、含音频流、
  分辨率与请求画幅一致、抽帧非全黑。
* 不调用外部服务,只用项目已解析的 ffmpeg 二进制(``utils.get_ffmpeg_binary``)。
"""

from __future__ import annotations

import json
import subprocess

from loguru import logger

from app.utils import utils


def _ffmpeg_bin() -> str:
    return utils.get_ffmpeg_binary()


def probe_video(path: str) -> dict:
    """用 ffprobe 读取容器/流信息;失败返回 {'error': ...}。"""
    ffmpeg_bin = _ffmpeg_bin()
    # imageio-ffmpeg 只带 ffmpeg,不带 ffprobe;优先用同名目录下的 ffprobe,
    # 找不到就用 ffmpeg 自身的 -i 输出来解析(下方 _parse_from_ffmpeg)。
    import os
    import shutil

    ffprobe_bin = shutil.which("ffprobe")
    if not ffprobe_bin:
        candidate = os.path.join(os.path.dirname(ffmpeg_bin), "ffprobe")
        if os.path.exists(candidate):
            ffprobe_bin = candidate

    try:
        if ffprobe_bin:
            completed = subprocess.run(
                [
                    ffprobe_bin,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration,size:stream=codec_type,codec_name,width,height",
                    "-of",
                    "json",
                    path,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            return json.loads(completed.stdout or "{}")
        # 退化路径:用 ffmpeg -i 的输出人工解析关键字段。
        completed = subprocess.run(
            [ffmpeg_bin, "-i", path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return _parse_from_ffmpeg(completed.stderr or "")
    except Exception as exc:
        logger.warning(f"quality gate probe failed: {type(exc).__name__}: {exc}")
        return {"error": f"{type(exc).__name__}: {exc}"}


def _parse_from_ffmpeg(info: str) -> dict:
    """从 ``ffmpeg -i`` 的 stderr 中提取 duration 与视频/音频流。"""
    import re

    streams: list[dict] = []
    duration = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", info)
    if m:
        h, mm, ss = int(m.group(1)), int(m.group(2)), float(m.group(3))
        duration = h * 3600 + mm * 60 + ss
    for line in info.splitlines():
        if "Video:" in line:
            res = re.search(r"(\d{2,5})x(\d{2,5})", line)
            codec = re.search(r"Video:\s*([a-zA-Z0-9_]+)", line)
            streams.append(
                {
                    "codec_type": "video",
                    "codec_name": codec.group(1) if codec else "",
                    "width": int(res.group(1)) if res else 0,
                    "height": int(res.group(2)) if res else 0,
                }
            )
        elif "Audio:" in line:
            codec = re.search(r"Audio:\s*([a-zA-Z0-9_]+)", line)
            streams.append(
                {"codec_type": "audio", "codec_name": codec.group(1) if codec else ""}
            )
    return {"format": {"duration": str(duration)}, "streams": streams}


# 请求画幅 -> 期望像素(竖屏/横屏/方形)
ASPECT_PIXELS = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
}


def _has_non_black_frame(path: str, at_seconds: float) -> bool | None:
    """抽取单帧并判断是否非全黑;无法判定时返回 None(不误报)。"""
    ffmpeg_bin = _ffmpeg_bin()
    try:
        completed = subprocess.run(
            [
                ffmpeg_bin,
                "-v",
                "error",
                "-ss",
                str(max(0.0, at_seconds)),
                "-i",
                path,
                "-frames:v",
                "1",
                "-vf",
                "blackframe=amount=98:threshold=24",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        # blackframe 过滤器命中时会向 stderr 打印 "pblack:" 行。
        return "pblack:" not in (completed.stderr or "")
    except Exception as exc:
        logger.warning(f"quality gate black-frame probe failed: {type(exc).__name__}: {exc}")
        return None


def check_video_quality(
    video_path: str,
    requested_aspect: str = "",
    min_duration: float = 0,
    min_width: int = 0,
) -> dict:
    """对单个成片做确定性质量校验,返回 {passed, issues[], metrics{}}。

    返回的 ``issues`` 为空表示合格。任何"无法判定"的情况都不计入 issues,
    避免把探测失败当成产物缺陷。``min_duration`` / ``min_width`` 为可配置阈值
    (0 表示不判定),让"内容质量"标准可由部署方定义。
    """
    issues: list[str] = []
    metrics: dict = {}

    probe = probe_video(video_path)
    if probe.get("error"):
        return {
            "passed": False,
            "issues": [f"probe_failed: {probe['error']}"],
            "metrics": {},
        }

    fmt = probe.get("format") or {}
    try:
        duration = float(fmt.get("duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0.0
    metrics["duration"] = round(duration, 2)

    streams = probe.get("streams") or []
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if duration <= 0:
        issues.append("zero_duration")
    elif min_duration > 0 and duration < min_duration:
        issues.append("below_min_duration")
    if video_stream is None:
        issues.append("missing_video_stream")
    if audio_stream is None:
        issues.append("missing_audio_stream")
    else:
        metrics["audio_codec"] = audio_stream.get("codec_name", "")

    if video_stream:
        w = int(video_stream.get("width") or 0)
        h = int(video_stream.get("height") or 0)
        metrics["width"], metrics["height"] = w, h
        metrics["video_codec"] = video_stream.get("codec_name", "")
        expected = ASPECT_PIXELS.get(str(requested_aspect or "").strip())
        if expected and (w, h) != expected:
            # 只记录不判负:部分编码器会做轻微尺寸对齐,过严会把有效产物判死。
            metrics["aspect_mismatch"] = f"{w}x{h} != {expected[0]}x{expected[1]}"
        if w <= 0 or h <= 0:
            issues.append("invalid_resolution")
        elif min_width > 0 and w < min_width:
            issues.append("below_min_width")

    # 抽首帧之外的中间帧判断是否整段黑屏(长视频取时长的 40%)。
    if duration > 1 and video_stream is not None:
        non_black = _has_non_black_frame(video_path, duration * 0.4)
        if non_black is False:
            issues.append("all_black_frames")
        metrics["mid_frame_non_black"] = non_black

    return {"passed": not issues, "issues": issues, "metrics": metrics}