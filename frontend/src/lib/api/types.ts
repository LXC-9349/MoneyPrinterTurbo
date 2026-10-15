// 前端 API 契约类型：严格镜像后端 `app/models/schema.py` 与各控制器返回。
// 目的——把 draft（VideoParams 快照）与向导四步用到的选项/人物/任务状态全部
// 静态建模，杜绝 any 蔓延；字段名与后端保持一致（snake_case），因为 draft 会
// 原样 PATCH 回 `/api/v1/projects/{id}` 交给 `VideoParams.model_validate` 校验。

// ── 枚举（与 schema.py 的 str Enum / Literal 对齐）────────────────────────
export type VideoAspect = "16:9" | "9:16" | "1:1";
export type VideoFitMode = "cover" | "contain";
export type VideoConcatMode = "random" | "sequential";
// transition：后端 VideoTransitionMode，none 序列化为 null，其余为字符串。
export type VideoTransitionMode =
  | "Shuffle"
  | "FadeIn"
  | "FadeOut"
  | "SlideIn"
  | "SlideOut"
  | "ZoomIn"
  | "ZoomOut";
export type SubtitleDisplayMode = "sentence" | "word_by_word";
export type SubtitleAnimation = "none" | "pop_spring";
export type ProjectMode = "biography" | "normal";
export type ProjectStatus = "draft" | "processing" | "done" | "failed";
export type WorkflowStep = "content" | "visual" | "audio" | "output";

// 向导步骤顺序（对外契约，与后端 project_store.WORKFLOW_STEPS 一致）。
export const WORKFLOW_STEPS: readonly WorkflowStep[] = [
  "content",
  "visual",
  "audio",
  "output",
] as const;

export const STEP_LABELS: Record<WorkflowStep, string> = {
  content: "内容",
  visual: "视觉",
  audio: "声音",
  output: "字幕与生成",
};

// ── draft 快照（VideoParams）──────────────────────────────────────────────
export interface MaterialInfo {
  provider: string;
  url: string;
  duration: number;
  source_info?: Record<string, unknown> | null;
}

export interface VideoParams {
  video_subject: string;
  video_script: string;
  video_terms: string | string[] | null;
  video_aspect: VideoAspect;
  video_fit_mode: VideoFitMode;
  video_concat_mode: VideoConcatMode;
  video_transition_mode: VideoTransitionMode | null;
  video_clip_duration: number;
  video_clip_speed: number;
  match_materials_to_script: boolean;
  video_count: number;
  video_source: string;
  video_materials: MaterialInfo[] | null;
  custom_audio_file: string | null;
  video_language: string;
  voice_name: string;
  voice_volume: number;
  voice_rate: number;
  bgm_type: string;
  bgm_file: string;
  bgm_volume: number;
  video_music_prompt: string;
  sonilo_bgm_prompt: string;
  subtitle_enabled: boolean;
  subtitle_position: string;
  subtitle_display_mode: SubtitleDisplayMode;
  subtitle_animation: SubtitleAnimation;
  custom_position: number;
  font_name: string;
  text_fore_color: string;
  text_background_color: boolean | string;
  rounded_subtitle_background: boolean;
  font_size: number;
  stroke_color: string;
  stroke_width: number;
  n_threads: number;
  paragraph_number: number;
  video_script_prompt: string;
  custom_system_prompt: string;
  biography_person: string;
  material_fallback_source: string;
  biography_script: Record<string, unknown> | null;
  biography_doc_ids: string[];
}

// ── project 条目 ─────────────────────────────────────────────────────────
// summary_projection 的产物（列表用，无 draft）。
export interface ProjectSummary {
  id: string;
  task_id: string;
  title: string;
  mode: ProjectMode;
  person: string;
  language: string;
  source: string;
  status: ProjectStatus;
  step: WorkflowStep;
  created_at: number | null;
  updated_at: number | null;
  has_draft: boolean;
}

// get_entry / create_project / patch 返回的完整条目（含 draft）。
export interface ProjectEntry {
  id: string;
  task_id: string;
  title: string;
  mode: ProjectMode;
  person: string;
  language: string;
  source: string;
  status: ProjectStatus;
  step: WorkflowStep;
  created_at: number;
  updated_at: number;
  draft: VideoParams;
}

// PATCH 请求体（ProjectPatchRequest）：全部可选，draft 为 VideoParams 形状。
export interface ProjectPatch {
  draft?: Partial<VideoParams>;
  step?: string;
  status?: string;
  title?: string;
  person?: string;
  language?: string;
  source?: string;
  bind_task_id?: string;
}

// ── options 端点返回 ─────────────────────────────────────────────────────
export interface Option {
  value: string;
  label: string;
}
export interface LanguagesResult {
  languages: Option[];
}
export interface VideoSourcesResult {
  groups: { id: string; label: string; sources: Option[] }[];
  sources: string[];
}
export interface FontsResult {
  fonts: Option[];
}
export interface BgmFile {
  name: string;
  size: number;
  file: string;
}
export interface BgmListResult {
  files: BgmFile[];
}
export interface VoicesResult {
  provider: string;
  providers: string[];
  voices: Option[];
}

// ── biography / persons ──────────────────────────────────────────────────
export interface Person {
  person: string;
  dynasty: string;
  category: string;
  doc_count: number;
  source: "library" | "custom";
  pinned: boolean;
  hidden: boolean;
  note: string;
  groups: string[];
}
export interface PersonsResult {
  persons: Person[];
  total: number;
  dynasties: string[];
  categories: string[];
}

// ── 工作台（以“人物 → 任务”为中心，/api/v1/workbench/*）───────────────────
// 人物卡片：在 Person 之上叠加一次聚合返回的统计数据（§二，杜绝 N+1）。
export interface PersonCard extends Person {
  video_count: number;
  article_count: number;
  article_enabled_count: number;
  active_task_count: number;
  awaiting_count: number;
  task_count: number;
  adopted: boolean;
  last_updated: string | null;
  used: boolean;
}

export interface DashboardResult {
  persons: PersonCard[];
  total: number;
  // 候选不再内联返回（首屏 HTML 控制），只带总数；列表走 /persons/candidates 分页。
  candidate_count: number;
  active_task_total: number;
  dynasties: string[];
  categories: string[];
  sort_options: string[];
}

// 史料库候选（未使用）服务端搜索 + 分页（每页 ≤50）。
export interface PersonCandidatesResult {
  candidates: PersonCard[];
  total: number;
  offset: number;
  limit: number;
  has_more: boolean;
}

// 新建人物时的史料库联想候选（§二）。
export interface PersonSuggestCandidate {
  person: string;
  dynasty: string;
  category: string;
  in_library: boolean;
  doc_count: number;
}
export interface PersonSuggestResult {
  query: string;
  candidates: PersonSuggestCandidate[];
}

// 任务表状态文本（task_repo status 列）。draft=草稿，非活动态。
export type WorkbenchTaskStatus =
  | "draft"
  | "queued"
  | "processing"
  | "awaiting_confirmation"
  | "completed"
  | "failed"
  | "cancelled";

// tasks 表行（§七）。params_json/artifacts_json/warnings_json 为后端原始 JSON 串，前端按需 parse。
export interface WorkbenchTask {
  task_id: string;
  person_id: number | null;
  person_name: string;
  title: string;
  stop_at: string;
  status: WorkbenchTaskStatus;
  stage: string;
  progress: number;
  error: string;
  failed_stage: string;
  parent_task_id: string;
  version: number;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  updated_at: string;
  params_json?: string;
  artifacts_json?: string;
  warnings_json?: string;
  state?: number; // /tasks/active 懒同步时并入的实时状态码
}
export interface WorkbenchTasksResult {
  tasks: WorkbenchTask[];
  total: number;
}
export interface ActiveTasksResult {
  tasks: WorkbenchTask[];
  count: number;
}
export interface PersonDocument {
  doc_id: string;
  category: string;
  title: string;
  n_chars: number;
}
export interface PersonDocumentsResult {
  person: string;
  dynasty: string;
  key_facts: string[];
  documents: PersonDocument[];
  doc_ids: string[];
  default_base_script_ids: string[];
}

// ── 素材库管理（人物 CRUD / 分组 / 统计）────────────────────────────────
// GET /library/counts：语料库各表计数；库未启用或缺表时为 null。
export interface LibraryCounts {
  documents: number | null;
  books: number | null;
  chapters: number | null;
  persons: number | null;
}

// GET /library/groups：自定义分组（含成员数）。
export interface PersonGroup {
  name: string;
  created_at: string;
  count: number;
}
export interface GroupsResult {
  groups: PersonGroup[];
}

// GET /library/persons/{name}/profile：叠加层感知的完整档案（文库或自建）。
// 供人物编辑器回填 facts/note 等列表接口未携带的字段。
export interface PersonProfile {
  person: string;
  dynasty: string | null;
  category?: string;
  doc_count?: number;
  key_facts: string[];
  note?: string;
  pinned?: boolean;
  groups?: string[];
  source?: "library" | "custom";
}

// POST /library/persons 请求体；PATCH 走 PersonPatch（字段全可选）。
export interface PersonCreateInput {
  person: string;
  dynasty?: string;
  category?: string;
  note?: string;
  facts?: string[];
}
export interface PersonPatchInput {
  dynasty?: string;
  category?: string;
  note?: string;
  facts?: string[];
  pinned?: boolean;
}

// ── 设置（全局配置读写 + 存储/缓存）───────────────────────
// GET /settings 视图：含 secret（api_key / provider key），仅供客户端直连读取，
// 不经 SSR 注入 HTML。llm_providers 镜像后端 LLM_PROVIDER_REGISTRY 的可展示子集。
export interface LlmProviderMeta {
  id: string;
  label: string;
  show_api_key: boolean;
  show_base_url: boolean;
  requires_model_name: boolean;
  requires_api_key: boolean;
  default_model: string;
  default_base_url: string;
  api_key_url: string;
  model_docs_url: string;
  service_endpoints: { id: string; label: string; base_url: string; api_key_url: string }[];
}

export interface SettingsLlmView {
  provider: string;
  api_key: string;
  base_url_override: string;
  base_url_effective: string;
  model_override: string;
  model_effective: string;
}

export interface SettingsUploadPost {
  enabled: boolean;
  auto_upload: boolean;
  api_key: string;
  username: string;
  platforms: string[];
  youtube_privacy_status: string;
  youtube_made_for_kids: boolean;
}

export interface SettingsView {
  llm: SettingsLlmView;
  llm_providers: LlmProviderMeta[];
  material: {
    pexels_api_keys: string;
    pixabay_api_keys: string;
    coverr_api_keys: string;
  };
  ui: { hide_log: boolean };
  api_key: string;
  upload_post: SettingsUploadPost;
}

// PUT /settings 请求体（SettingsUpdateRequest）：全部可选，仅写传入的字段。
// 素材 key 以逗号分隔字符串传入；base_url/model_name 命中默认值后端会归一化为空。
export interface SettingsUpdateInput {
  llm_provider?: string;
  llm_api_key?: string;
  llm_base_url?: string;
  llm_model_name?: string;
  pexels_api_keys?: string;
  pixabay_api_keys?: string;
  coverr_api_keys?: string;
  api_key?: string;
  hide_log?: boolean;
  upload_post_enabled?: boolean;
  upload_post_auto_upload?: boolean;
  upload_post_api_key?: string;
  upload_post_username?: string;
  upload_post_platforms?: string[];
  upload_post_youtube_privacy_status?: string;
  upload_post_youtube_made_for_kids?: boolean;
}

export interface LlmTestResult {
  ok: boolean;
  error: string;
  elapsed: number;
}

// GET /storage/usage：各托管子目录占用 + 视频缓存统计。
export interface StorageDirUsage {
  name: string;
  count: number;
  size: number;
  exists: boolean;
}
export interface VideoCacheSummary {
  file_count: number;
  total_size: number;
  oldest_mtime?: number | null;
  newest_mtime?: number | null;
}
export interface StorageUsage {
  dirs: StorageDirUsage[];
  total_bytes: number;
  video_cache_dir: string;
  video_cache: VideoCacheSummary;
}

export interface CachePreview {
  file_count: number;
  total_size: number;
}
export interface CacheCleanResult {
  deleted_count: number;
  deleted_size: number;
  failed_count: number;
  remaining: { file_count: number; total_size: number };
}

// ── 任务（生成 + 轮询）──────────────────────────────────────────────────
// create_task 成功返回体（get_response 的 data）。
export interface TaskCreated {
  task_id: string;
  request_id: string;
  params: Record<string, unknown>;
}

// 任务状态码（app/models/const.py）：4 processing / 1 complete / -1 failed。
// 轮询终止条件：state 进入 COMPLETE 或 FAILED。
export const TaskState = {
  PROCESSING: 4,
  COMPLETE: 1,
  FAILED: -1,
} as const;

export interface TaskStatus {
  task_id: string;
  state: number;
  progress: number;
  videos?: string[] | null;
  combined_videos?: string[] | null;
  failed_stage?: string | null;
  error?: string | null;
  cross_post_state?: "pending" | "processing" | "complete" | "failed" | null;
  cross_post_results?: Record<string, unknown>[] | null;
  cross_post_error?: string | null;
  [key: string]: unknown;
}

// ── LLM 生成端点返回 ─────────────────────────────────────────────────────
export interface ScriptResult {
  video_script: string;
}
export interface TermsResult {
  video_terms: string[];
}

// ── TTS 试听返回 ─────────────────────────────────────────────────────────
export interface TtsPreviewResult {
  audio_url: string;
  duration: number | null;
  voice_name: string;
}

// ── 统一响应信封（utils.get_response）───────────────────────────────────
export interface ApiEnvelope<T> {
  status: number;
  data?: T;
  message?: string;
}
