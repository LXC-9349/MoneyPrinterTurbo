"use server";

// SolidStart 服务端读代理。用 "use server" 指令把本模块的每个导出编译成可在
// SSR 与客户端 RPC 调用的服务端函数；它们在 SolidStart 进程内用 API_INTERNAL_BASE
// 访问 FastAPI（服务器对服务器，天然同源，绕开浏览器 CORS），符合计划的混合数据模式。
//
// 注意："use server" 模块的导出必须是函数；下面的私有 helper 不导出，留在服务端闭包内。
import type {
  ApiEnvelope,
  BgmListResult,
  FontsResult,
  GroupsResult,
  LanguagesResult,
  LibraryCounts,
  PersonDocumentsResult,
  PersonsResult,
  ProjectEntry,
  VideoSourcesResult,
  VoicesResult,
} from "~/lib/api/types";

function internalBase(): string {
  const raw = import.meta.env.API_INTERNAL_BASE ?? "http://127.0.0.1:8917";
  return raw.replace(/\/+$/, "");
}

async function read<T>(path: string): Promise<T> {
  const res = await fetch(`${internalBase()}/api/v1${path}`, {
    headers: { accept: "application/json" },
    // 让开发期上游故障能快速失败，而不是拖到 SSR 超时。
    signal: AbortSignal.timeout(30000),
  });
  if (!res.ok) {
    throw new Error(`upstream ${res.status} for ${path}`);
  }
  const json = (await res.json()) as ApiEnvelope<T>;
  if (json.status >= 400) {
    throw new Error(json.message ?? `upstream error ${json.status}`);
  }
  return json.data as T;
}

function q(params: Record<string, string | number | boolean | undefined>): string {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v == null || v === "") continue;
    usp.set(k, String(v));
  }
  const s = usp.toString();
  return s ? `?${s}` : "";
}

export async function readProjects(): Promise<{ projects: unknown[]; total: number }> {
  return read("/projects");
}

export async function readProject(projectId: string): Promise<ProjectEntry> {
  return read(`/projects/${encodeURIComponent(projectId)}`);
}

export async function readVoices(provider: string): Promise<VoicesResult> {
  return read(`/options/voices${q({ provider })}`);
}

export async function readVideoSources(): Promise<VideoSourcesResult> {
  return read("/options/video-sources");
}

export async function readFonts(): Promise<FontsResult> {
  return read("/options/fonts");
}

export async function readBgmList(): Promise<BgmListResult> {
  return read("/musics");
}

export async function readLanguages(): Promise<LanguagesResult> {
  return read("/options/languages");
}

export async function readPersons(filter: {
  dynasty?: string;
  category?: string;
  keyword?: string;
  group?: string;
  include_hidden?: boolean;
}): Promise<PersonsResult> {
  return read(`/library/persons${q(filter)}`);
}

export async function readLibraryCounts(): Promise<LibraryCounts> {
  return read("/library/counts");
}

export async function readGroups(): Promise<GroupsResult> {
  return read("/library/groups");
}

export async function readPersonDocuments(name: string): Promise<PersonDocumentsResult> {
  return read(`/library/persons/${encodeURIComponent(name)}/documents`);
}
