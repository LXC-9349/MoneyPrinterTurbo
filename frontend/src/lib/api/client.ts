// 客户端直连 FastAPI：写操作 + 生成 + 轮询 + 上传 + 试听（浏览器侧执行）。
// 读走 `~/server/queries` 的 "use server" 代理（同源、SSR）；这里刻意用直连以
// 落实计划的"混合模式"，因此开发期需在启动后端时注入
//   CORS_ALLOWED_ORIGINS=http://localhost:7310
// （见 frontend/.env.example）。PUBLIC_API_BASE 必须以 PUBLIC_ 前缀暴露给浏览器。
import type {
  ActiveTasksResult,
  ApiEnvelope,
  Article,
  ArticleCreateInput,
  ArticlePatchInput,
  ArticlesResult,
  CacheCleanResult,
  CachePreview,
  DraftSaveInput,
  GroupsResult,
  LibraryArticlesResult,
  LibraryLinkPatchInput,
  LlmTestResult,
  PersonCreateInput,
  PersonAssetItem,
  PersonDetailResult,
  PersonFactItem,
  PersonAliasItem,
  PersonPatchInput,
  PersonProfile,
  PersonCandidatesResult,
  ProjectEntry,
  ProjectMode,
  ProjectPatch,
  QueueStats,
  ScriptResult,
  SettingsUpdateInput,
  SettingsView,
  StorageUsage,
  TaskCreated,
  TaskCompliance,
  TaskDetailResult,
  TaskStatus,
  TermsResult,
  TtsPreviewResult,
  VideoParams,
  WorkbenchTask,
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

// ── 里程碑 3 #2：任务列表批量操作 / 重试 / 导出 ────────────────────
export type TaskBatchAction = "cancel" | "archive" | "unarchive" | "delete";
export interface TaskBatchResult {
  action: string;
  affected: string[];
  count: number;
}

export function batchTasks(
  taskIds: string[],
  action: TaskBatchAction,
): Promise<TaskBatchResult> {
  return request<TaskBatchResult>("POST", "/workbench/tasks/batch", {
    task_ids: taskIds,
    action,
  });
}

export function retryTask(
  taskId: string,
): Promise<{ task: WorkbenchTask; retry_of: string; version: number }> {
  return request<{ task: WorkbenchTask; retry_of: string; version: number }>(
    "POST",
    `/workbench/tasks/${enc(taskId)}/retry`,
  );
}

// 导出成片：浏览器直链下载（GET 文件），拼接绝对 URL供 <a download> 使用。
export function exportTaskUrl(taskId: string): string {
  return resolveApiUrl(`/api/v1/workbench/tasks/${enc(taskId)}/export`);
}

// ── 里程碑 3 #3：任务详情（阶段时间线 / 版本链 / 参数产物 / 日志）────────
export function getTaskDetail(taskId: string): Promise<TaskDetailResult> {
  return request<TaskDetailResult>("GET", `/workbench/tasks/${enc(taskId)}`);
}

// 详情页内嵌播放器：inline 流绝对 URL（无 attachment，供 <video src> 直接播放）。
export function taskVideoUrl(taskId: string): string {
  return resolveApiUrl(`/api/v1/workbench/tasks/${enc(taskId)}/video`);
}

// “我已知晓”合规提醒（里程碑 3 #7）：确认后才解禁导出，返回最新 compliance 块。
export function acknowledgeCompliance(
  taskId: string,
): Promise<{ task_id: string; compliance: TaskCompliance }> {
  return request<{ task_id: string; compliance: TaskCompliance }>(
    "POST",
    `/workbench/tasks/${enc(taskId)}/acknowledge-compliance`,
  );
}

// 从失败阶段继续（里程碑 3 #4）：同一 task_id 重新派发，复用已生成的中间产物。
export function resumeTask(taskId: string): Promise<{ task: WorkbenchTask; resumed: boolean }> {
  return request<{ task: WorkbenchTask; resumed: boolean }>(
    "POST",
    `/workbench/tasks/${enc(taskId)}/resume`,
  );
}

// ── 队列控制（里程碑 3 #5）──────────────────────────────────
export function fetchQueue(): Promise<QueueStats> {
  return request<QueueStats>("GET", "/workbench/queue");
}

export function pauseQueue(): Promise<QueueStats> {
  return request<QueueStats>("POST", "/workbench/queue/pause");
}

export function resumeQueue(): Promise<QueueStats> {
  return request<QueueStats>("POST", "/workbench/queue/resume");
}

export function setQueueConcurrency(n: number): Promise<QueueStats> {
  return request<QueueStats>("PATCH", "/workbench/queue", { max_concurrent_tasks: n });
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

// ── 里程碑 2：人物详情聚合 / 文章 CRUD / 草稿（工作台子前缀）───────────
export function fetchPersonDetail(name: string): Promise<PersonDetailResult> {
  return request<PersonDetailResult>("GET", `/workbench/persons/${enc(name)}/detail`);
}

export function fetchLibraryArticles(name: string): Promise<LibraryArticlesResult> {
  return request<LibraryArticlesResult>("GET", `/workbench/persons/${enc(name)}/library-articles`);
}

export function fetchArticles(name: string): Promise<ArticlesResult> {
  return request<ArticlesResult>("GET", `/workbench/persons/${enc(name)}/articles`);
}

export function addArticle(name: string, input: ArticleCreateInput): Promise<Article> {
  return request<Article>("POST", `/workbench/persons/${enc(name)}/articles`, input);
}

export function patchArticle(articleId: number, input: ArticlePatchInput): Promise<Article> {
  return request<Article>("PATCH", `/workbench/articles/${articleId}`, input);
}

export function deleteArticle(articleId: number): Promise<{ article_id: number; deleted: boolean }> {
  return request<{ article_id: number; deleted: boolean }>("DELETE", `/workbench/articles/${articleId}`);
}

// 史料库软引用行的「用于生成」开关/权重（里程碑 2 收尾 #6，状态存 library_links）。
export function patchPersonLibraryLink(
  name: string,
  docId: string,
  input: LibraryLinkPatchInput,
): Promise<{ person: string; doc_id: string; enabled: boolean | null; weight: number | null }> {
  return request("PATCH", `/workbench/persons/${enc(name)}/library-links/${enc(docId)}`, input);
}

export function saveDraft(input: DraftSaveInput): Promise<WorkbenchTask> {
  return request<WorkbenchTask>("POST", "/workbench/drafts", input);
}

export function deleteDraft(draftId: string): Promise<{ draft_id: string; deleted: boolean }> {
  return request<{ draft_id: string; deleted: boolean }>(
    "DELETE",
    `/workbench/drafts/${enc(draftId)}`,
  );
}

// ── 事实 CRUD（里程碑 2 收尾 #4：增删改 + 采纳/忽略）────────────────
export function addPersonFact(name: string, statement: string, sourceRef = ""): Promise<PersonFactItem> {
  return request<PersonFactItem>("POST", `/workbench/persons/${enc(name)}/facts`, {
    statement,
    source_ref: sourceRef,
  });
}

export function patchPersonFact(
  factId: number,
  input: { statement?: string; verified?: boolean; human_confirmed?: boolean },
): Promise<PersonFactItem> {
  return request<PersonFactItem>("PATCH", `/workbench/facts/${factId}`, input);
}

export function deletePersonFact(factId: number): Promise<{ fact_id: number; deleted: boolean }> {
  return request<{ fact_id: number; deleted: boolean }>("DELETE", `/workbench/facts/${factId}`);
}

// ── 人物别名（里程碑 2 收尾 #5 编辑资料弹窗）───────────────────────
export function addPersonAlias(name: string, alias: string): Promise<{ person: string; aliases: PersonAliasItem[] }> {
  return request<{ person: string; aliases: PersonAliasItem[] }>(
    "POST",
    `/workbench/persons/${enc(name)}/aliases`,
    { alias },
  );
}

export function removePersonAlias(name: string, alias: string): Promise<{ person: string; aliases: PersonAliasItem[] }> {
  return request<{ person: string; aliases: PersonAliasItem[] }>(
    "DELETE",
    `/workbench/persons/${enc(name)}/aliases/${enc(alias)}`,
  );
}

// ── 人物图片素材操作（里程碑 2 收尾 #3：封面/排除/上传）──────────────
export function setPersonAssetCover(name: string, assetId: number): Promise<{ person: string; asset_id: number; is_cover: boolean }> {
  return request<{ person: string; asset_id: number; is_cover: boolean }>(
    "POST",
    `/workbench/persons/${enc(name)}/assets/${assetId}/cover`,
  );
}

export function patchPersonAsset(
  name: string,
  assetId: number,
  state: "pending" | "approved" | "excluded",
): Promise<{ person: string; asset_id: number; state: string }> {
  return request<{ person: string; asset_id: number; state: string }>(
    "PATCH",
    `/workbench/persons/${enc(name)}/assets/${assetId}`,
    { state },
  );
}

export function uploadPersonImage(name: string, file: File): Promise<PersonAssetItem> {
  return upload<PersonAssetItem>(`/workbench/persons/${enc(name)}/assets/upload`, file);
}
