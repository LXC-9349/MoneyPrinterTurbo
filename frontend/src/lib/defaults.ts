// 里程碑 2 · 创建任务弹窗的默认参数与预设。
// defaultVideoParams 镜像后端 app/models/schema.py 的 VideoParams 字段默认值，
// 保证前端提交的快照能通过 TaskVideoRequest 校验，且与后端空白 draft 一致。
// 传记（人物详情）入口固定 video_source=historical_archive，与 project_store
// .create_project("biography") 的取材源约定相同。
import type { VideoParams } from "~/lib/api/types";

export function defaultVideoParams(person = ""): VideoParams {
  return {
    video_subject: person,
    video_script: "",
    video_terms: null,
    video_aspect: "9:16",
    video_fit_mode: "cover",
    video_concat_mode: "random",
    video_transition_mode: null,
    video_clip_duration: 5,
    video_clip_speed: 1.0,
    match_materials_to_script: false,
    video_count: 1,
    video_source: person ? "historical_archive" : "pexels",
    video_materials: null,
    custom_audio_file: null,
    video_language: "",
    voice_name: "zh-CN-XiaoxiaoNeural-Female",
    voice_volume: 1.0,
    voice_rate: 1.0,
    bgm_type: "random",
    bgm_file: "",
    bgm_volume: 0.2,
    video_music_prompt: "",
    sonilo_bgm_prompt: "",
    subtitle_enabled: true,
    subtitle_position: "bottom",
    subtitle_display_mode: "sentence",
    subtitle_animation: "none",
    custom_position: 70,
    font_name: "STHeitiMedium.ttc",
    text_fore_color: "#FFFFFF",
    text_background_color: false,
    rounded_subtitle_background: false,
    font_size: 60,
    stroke_color: "#000000",
    stroke_width: 1.5,
    n_threads: 2,
    paragraph_number: 1,
    video_script_prompt: "",
    custom_system_prompt: "",
    biography_person: person,
    material_fallback_source: "pexels",
    biography_script: null,
    biography_doc_ids: [],
  };
}

// 预设：一键覆盖若干常用参数分组（合并进当前草稿）。用户可在此基础上再微调。
export interface Preset {
  id: string;
  label: string;
  hint: string;
  patch: Partial<VideoParams>;
}

export const PRESETS: Preset[] = [
  {
    id: "default",
    label: "标准",
    hint: "竖屏 9:16 · 单条 · 5s 片段 · 自动配乐",
    patch: {
      video_aspect: "9:16",
      video_count: 1,
      video_clip_duration: 5,
      bgm_type: "random",
      bgm_volume: 0.2,
      subtitle_enabled: true,
    },
  },
  {
    id: "landscape",
    label: "横屏成片",
    hint: "横屏 16:9 · 语速稍缓 · 更强调字幕",
    patch: {
      video_aspect: "16:9",
      video_count: 1,
      video_clip_duration: 6,
      voice_rate: 0.95,
      bgm_volume: 0.15,
      subtitle_enabled: true,
      font_size: 48,
    },
  },
  {
    id: "short",
    label: "快节奏短视频",
    hint: "竖屏 · 2 条候选 · 2.5s 快切 · 匹配文案",
    patch: {
      video_aspect: "9:16",
      video_count: 2,
      video_clip_duration: 3,
      match_materials_to_script: true,
      bgm_volume: 0.25,
      voice_rate: 1.1,
    },
  },
];
