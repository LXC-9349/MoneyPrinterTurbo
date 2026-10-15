// 客户端直连 FastAPI：写操作 + 生成 + 轮询 + 上传 + 试听（浏览器侧执行）。
// 读走 `~/server/queries` 的 "use server" 代理（同源、SSR）；这里刻意用直连以
// 落实计划的"混合模式"，因此开发期需在启动后端时注入
//   CORS_ALLOWED_ORIGINS=http://localhost:7310
// （见 frontend/.env.example）。PUBLIC_API_BASE 必须以 PUBLIC_ 前缀暴露给浏览器。
import type {
  ActiveTasksResult,
  ApiEnvelope,
  CacheCleanResult,
  CachePreview,
  GroupsResult,
  LlmTestResult,
  PersonCreateInput,
  PersonPatchInput,
  PersonProfile,
  PersonCandidatesResult,
  ProjectEntry,
  ProjectMode,
  ProjectPatch,
  ScriptResult,
  SettingsUpdateInput,
  SettingsView,
  StorageUsage,
  TaskCreated,
  TaskStatus,
  TermsResult,
  TtsPreviewResult,
  VideoParams,
} from "./types";

export class ApiError extends Error {
  status: number;
  data?: unknown;
  constructor(status: number, message: string, data?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.data = data;
  }
}

function baseUrl(): string {
  const raw = import.meta.env.PUBLIC_API_BASE ?? "";
  return raw.replace(/\/+$/, "");
}

/** 把后端返回的相对路径（/tasks/...、/api/v1/tts/preview/...）解析成绝对 URL。 */
export function resolveApiUrl(pathOrUrl: string): string {
  if (/^https?:\/\//i.test(pathOrUrl)) return pathOrUrl;
  return `${baseUrl()}/${pathOrUrl.replace(/^\/+/, "")}`;
}

// 统一解包：HTTP 非 2xx 或信封 status>=400 都抛 ApiError，message 用后端文案。
async function request<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const res = await fetch(`${baseUrl()}/api/v1${path}`, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  let json: ApiEnvelope<T> | undefined;
  try {
    json = (await res.json()) as ApiEnvelope<T>;
  } catch {
    // 非 JSON（极少见）：按 HTTP 状态归类。
  }
  const code = json?.status ?? res.status;
  if (!res.ok || code >= 400) {
    throw new ApiError(code, json?.message ?? `请求失败（${res.status}）`, json?.data);
  }
  return json?.data as T;
}

// ── projects 写 ──────────────────────────────────────────────────────────
export function createProject(mode: ProjectMode, title = ""): Promise<ProjectEntry> {
  return request<ProjectEntry>("POST", "/projects", { mode, title });
}

export function patchProject(id: string, patch: ProjectPatch): Promise<ProjectEntry> {
  return request<ProjectEntry>("PATCH", `/projects/${encodeURIComponent(id)}`, patch);
}

export function deleteProject(id: string): Promise<{ id: string; deleted: boolean }> {
  return request<{ id: string; deleted: boolean }>(
    "DELETE",
    `/projects/${encodeURIComponent(id)}`,
  );
}

// ── 生成 + 轮询 ──────────────────────────────────────────────────────────
export function createVideoTask(params: VideoParams): Promise<TaskCreated> {
  return request<TaskCreated>("POST", "/videos", params);
}

export function getTask(taskId: string): Promise<TaskStatus> {
  return request<TaskStatus>("GET", `/tasks/${encodeURIComponent(taskId)}`);
}

// ── LLM 生成（内容步）────────────────────────────────────────────────────
export function generateScript(input: {
  video_subject: string;
  video_language?: string;
  paragraph_number?: number;
  video_script_prompt?: string;
  custom_system_prompt?: string;
}): Promise<ScriptResult> {
  return request<ScriptResult>("POST", "/scripts", input);
}

export function generateTerms(input: {
  video_subject: string;
  video_script: string;
  amount?: number;
  match_materials_to_script?: boolean;
}): Promise<TermsResult> {
  return request<TermsResult>("POST", "/terms", input);
}

// ── TTS 试听 ─────────────────────────────────────────────────────────────
export function ttsPreview(input: {
  text: string;
  voice_name: string;
  voice_rate: number;
  voice_volume: number;
}): Promise<TtsPreviewResult> {
  return request<TtsPreviewResult>("POST", "/tts/preview", input);
}

// ── 素材 / BGM 上传（multipart，浏览器直连）──────────────────────────────
async function upload<T>(path: string, file: File): Promise<T> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${baseUrl()}/api/v1${path}`, { method: "POST", body: form });
  const json = (await res.json()) as ApiEnvelope<T>;
  const code = json?.status ?? res.status;
  if (!res.ok || code >= 400) {
    throw new ApiError(code, json?.message ?? `上传失败（${res.status}）`, json?.data);
  }
  return json?.data as T;
}

export function uploadVideoMaterial(file: File): Promise<{ file: string }> {
  return upload<{ file: string }>("/video_materials", file);
}

export function uploadBgm(file: File): Promise<{ file: string }> {
  return upload<{ file: string }>("/musics", file);
}

// ── 素材库：人物 CRUD / 分组 / 档案（写走客户端直连）───────────────────
const enc = encodeURIComponent;

export function createPerson(input: PersonCreateInput): Promise<PersonProfile> {
  return request<PersonProfile>("POST", "/library/persons", input);
}

export function patchPerson(name: string, patch: PersonPatchInput): Promise<PersonProfile> {
  return request<PersonProfile>("PATCH", `/library/persons/${enc(name)}`, patch);
}

export function deletePerson(name: string): Promise<{ person: string; outcome: string }> {
  return request<{ person: string; outcome: string }>("DELETE", `/library/persons/${enc(name)}`);
}

export function restorePerson(name: string): Promise<{ person: string; restored: boolean }> {
  return request<{ person: string; restored: boolean }>(
    "POST",
    `/library/persons/${enc(name)}/restore`,
  );
}

export function setPersonGroups(name: string, groups: string[]): Promise<{ person: string; groups: string[] }> {
  return request<{ person: string; groups: string[] }>(
    "PUT",
    `/library/persons/${enc(name)}/groups`,
    { groups },
  );
}

export function createGroup(name: string): Promise<GroupsResult> {
  return request<GroupsResult>("POST", "/library/groups", { name });
}

export function deleteGroup(name: string): Promise<{ deleted: boolean } & GroupsResult> {
  return request<{ deleted: boolean } & GroupsResult>("DELETE", `/library/groups/${enc(name)}`);
}

// 档案为按需读取（编辑器回填 facts/note），走客户端 GET 避免新增 SSR query。
export function fetchPersonProfile(name: string): Promise<PersonProfile> {
  return request<PersonProfile>("GET", `/library/persons/${enc(name)}/profile`);
}

// ── 工作台：顶栏“进行中任务”抽屉轮询（§一；2–5s 客户端直连，不进 SSR 缓存）──
export function fetchActiveTasks(): Promise<ActiveTasksResult> {
  return request<ActiveTasksResult>("GET", "/workbench/tasks/active");
}

export function cancelTask(taskId: string): Promise<{ task_id: string; cancelled: boolean }> {
  return request<{ task_id: string; cancelled: boolean }>(
    "POST",
    `/workbench/tasks/${enc(taskId)}/cancel`,
  );
}

// 史料库候选人物：服务端搜索 + 分页（每页 ≤50），浏览器直连、不进 SSR 缓存。
export function fetchWorkbenchCandidates(params: {
  keyword?: string;
  dynasty?: string;
  category?: string;
  group?: string;
  offset?: number;
  limit?: number;
}): Promise<PersonCandidatesResult> {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v != null && v !== "") usp.set(k, String(v));
  }
  const qs = usp.toString();
  return request<PersonCandidatesResult>("GET", `/workbench/persons/candidates${qs ? `?${qs}` : ""}`);
}

// ── 设置：全局配置读写 + LLM 连接测试（含 secret，一律客户端直连，不经 SSR）──
export function fetchSettings(): Promise<SettingsView> {
  return request<SettingsView>("GET", "/settings");
}

export function updateSettings(input: SettingsUpdateInput): Promise<SettingsView> {
  return request<SettingsView>("PUT", "/settings", input);
}

export function testLlmConnection(): Promise<LlmTestResult> {
  return request<LlmTestResult>("POST", "/settings/llm/test");
}

// ── 存储/缓存管理（占用统计与清理）─────────────────────────────
export function fetchStorageUsage(): Promise<StorageUsage> {
  return request<StorageUsage>("GET", "/storage/usage");
}

export function previewVideoCache(maxAgeDays?: number): Promise<CachePreview> {
  const qs = maxAgeDays ? `?max_age_days=${maxAgeDays}` : "";
  return request<CachePreview>("GET", `/storage/video-cache/preview${qs}`);
}

export function cleanVideoCache(maxAgeDays?: number): Promise<CacheCleanResult> {
  return request<CacheCleanResult>("POST", "/storage/video-cache/clean", {
    max_age_days: maxAgeDays ?? null,
  });
}
