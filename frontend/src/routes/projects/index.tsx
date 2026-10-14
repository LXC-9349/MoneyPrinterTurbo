// 项目列表：读走 query（SSR 预取 + 客户端缓存），新建走客户端直连 POST /projects，
// 删除走客户端 DELETE 后 revalidate 列表。卡片点击跳到 /projects/{id}（由该路由
// 重定向到条目当前步）。新建区分「传记 / 普通」两种模式。
import { For, Show, createSignal } from "solid-js";
import { Title } from "@solidjs/meta";
import { A, createAsync, useNavigate } from "@solidjs/router";
import { Button } from "~/components/ui/button";
import { Card, CardContent } from "~/components/ui/card";
import { Badge, type BadgeTone } from "~/components/ui/badge";
import { notify } from "~/components/ui/toast";
import { invalidateProjects, projectsQuery } from "~/lib/api/data";
import { ApiError, createProject, deleteProject } from "~/lib/api/client";
import { STEP_LABELS, type ProjectStatus } from "~/lib/api/types";

const STATUS_TONE: Record<ProjectStatus, BadgeTone> = {
  draft: "neutral",
  processing: "primary",
  done: "ok",
  failed: "err",
};
const STATUS_LABEL: Record<ProjectStatus, string> = {
  draft: "草稿",
  processing: "生成中",
  done: "已完成",
  failed: "失败",
};

function formatDate(ts: number | null): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
    d.getDate(),
  ).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(
    d.getMinutes(),
  ).padStart(2, "0")}`;
}

export default function ProjectsIndex() {
  // 列表始终来自 query（SSR 预取 + 客户端缓存）。
  const projects = createAsync(() => projectsQuery());
  const navigate = useNavigate();
  const [busy, setBusy] = createSignal(false);

  const start = async (mode: "biography" | "normal") => {
    setBusy(true);
    try {
      const entry = await createProject(mode);
      invalidateProjects();
      navigate(`/projects/${entry.id}/content`);
    } catch (e) {
      notify.error("新建失败", e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: string, title: string, ev: MouseEvent) => {
    ev.preventDefault();
    ev.stopPropagation();
    if (!window.confirm(`确认删除「${title || id}」？此操作不可撤销。`)) return;
    try {
      await deleteProject(id);
      invalidateProjects();
      notify.success("已删除项目");
    } catch (e) {
      notify.error("删除失败", e instanceof ApiError ? e.message : String(e));
    }
  };

  return (
    <main class="mx-auto max-w-5xl px-6 py-8">
      <Title>项目工作区 · MoneyPrinterTurbo</Title>
      <header class="mb-6 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 class="text-2xl font-semibold text-ink">项目工作区</h1>
          <p class="mt-1 text-sm text-ink-dim">
            以向导方式创建短视频：内容 → 视觉 → 声音 → 字幕与生成。
          </p>
        </div>
        <div class="flex gap-2">
          <Button variant="primary" disabled={busy()} onClick={() => void start("normal")}>
            ＋ 普通项目
          </Button>
          <Button variant="secondary" disabled={busy()} onClick={() => void start("biography")}>
            ＋ 传记项目
          </Button>
        </div>
      </header>

      <Show
        when={projects()}
        fallback={<p class="text-sm text-ink-dim">正在加载项目…</p>}
      >
        {(data) => (
          <Show
            when={data().projects.length > 0}
            fallback={
              <Card>
                <CardContent class="py-10 text-center text-sm text-ink-dim">
                  还没有项目，点击右上角新建一个开始吧。
                </CardContent>
              </Card>
            }
          >
            <ul class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              <For each={data().projects}>
                {(p) => (
                  <li>
                    <A
                      href={`/projects/${p.id}`}
                      class="block h-full rounded-card border border-line bg-surface p-4 shadow-card transition-colors hover:border-primary/60"
                    >
                      <div class="flex items-start justify-between gap-2">
                        <p class="line-clamp-2 flex-1 text-sm font-semibold text-ink">
                          {p.title ||
                            (p.mode === "biography" ? p.person || "传记项目" : "未命名项目")}
                        </p>
                        <Badge tone={STATUS_TONE[p.status]}>{STATUS_LABEL[p.status]}</Badge>
                      </div>
                      <dl class="mt-3 flex flex-col gap-1 text-xs text-ink-dim">
                        <div class="flex justify-between">
                          <dt>模式</dt>
                          <dd class="text-ink">{p.mode === "biography" ? "传记" : "普通"}</dd>
                        </div>
                        <div class="flex justify-between">
                          <dt>当前步</dt>
                          <dd class="text-ink">{STEP_LABELS[p.step]}</dd>
                        </div>
                        <div class="flex justify-between">
                          <dt>更新时间</dt>
                          <dd class="text-ink">{formatDate(p.updated_at)}</dd>
                        </div>
                      </dl>
                      <div class="mt-3 flex justify-end">
                        <button
                          type="button"
                          class="text-xs text-err hover:underline"
                          onClick={(ev) => void remove(p.id, p.title, ev)}
                        >
                          删除
                        </button>
                      </div>
                    </A>
                  </li>
                )}
              </For>
            </ul>
          </Show>
        )}
      </Show>
    </main>
  );
}
