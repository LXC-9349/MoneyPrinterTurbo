// 缓存化读层：把 "use server" 读代理用 @solidjs/router 的 query() 包一层，
// 得到「SSR 预取 → 序列化到客户端 → 客户端命中缓存 → 写操作后 revalidate(name)」
// 的能力。query 的 name 即 revalidate 的 key。路由用 createAsync(() => xQuery(...))
// 触发流式解析。
import { query, revalidate } from "@solidjs/router";
import {
  readBgmList,
  readFonts,
  readGroups,
  readLanguages,
  readLibraryCounts,
  readPersonDocuments,
  readPersons,
  readProject,
  readProjects,
  readVideoSources,
  readVoices,
} from "~/server/queries";
import type {
  BgmListResult,
  FontsResult,
  GroupsResult,
  LanguagesResult,
  LibraryCounts,
  PersonDocumentsResult,
  PersonsResult,
  ProjectEntry,
  ProjectSummary,
  VideoSourcesResult,
  VoicesResult,
} from "./types";

export interface ProjectsResult {
  projects: ProjectSummary[];
  total: number;
}

export const projectsQuery = query(
  readProjects as unknown as () => Promise<ProjectsResult>,
  "projects",
);
export const projectQuery = query(
  readProject as unknown as (id: string) => Promise<ProjectEntry>,
  "project",
);
export const voicesQuery = query(
  readVoices as unknown as (provider: string) => Promise<VoicesResult>,
  "voices",
);
export const videoSourcesQuery = query(
  readVideoSources as unknown as () => Promise<VideoSourcesResult>,
  "video-sources",
);
export const fontsQuery = query(
  readFonts as unknown as () => Promise<FontsResult>,
  "fonts",
);
export const bgmsQuery = query(
  readBgmList as unknown as () => Promise<BgmListResult>,
  "bgms",
);
export const languagesQuery = query(
  readLanguages as unknown as () => Promise<LanguagesResult>,
  "languages",
);
export const personsQuery = query(
  readPersons as unknown as (filter: {
    dynasty?: string;
    category?: string;
    keyword?: string;
    group?: string;
    include_hidden?: boolean;
  }) => Promise<PersonsResult>,
  "persons",
);
export const libraryCountsQuery = query(
  readLibraryCounts as unknown as () => Promise<LibraryCounts>,
  "library-counts",
);
export const groupsQuery = query(
  readGroups as unknown as () => Promise<GroupsResult>,
  "groups",
);
export const personDocsQuery = query(
  readPersonDocuments as unknown as (name: string) => Promise<PersonDocumentsResult>,
  "person-docs",
);

// 写操作后按需失效：只重新拉取「列表」query。
// 注意：绝不 revalidate("project")——详情页当前挂载的 WorkspaceProvider 从
// projectQuery 取初始 draft，一旦被 revalidate 触发 entry() 返回新引用，会使
// Show 重渲染→provider 重挂→丢失未保存编辑与进行中的轮询。save/轮询已把
// 服务端权威响应写回本地 store，无需再刷新详情 query。
export { revalidate };
export function invalidateProjects(): void {
  void revalidate("projects");
}

// 素材库人物/分组变更后按需失效（人物列表 + 分组 + 统计）。
export function invalidateLibrary(): void {
  void revalidate("persons");
  void revalidate("groups");
  void revalidate("library-counts");
}
