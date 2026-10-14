// 人物传记详情占位页（/persons/:name）。里程碑 2 交付完整五标签页（文章/任务/
// 成片/事实/图片素材）+ 右侧进行中任务栏 + 创建任务弹窗。本页先承接卡片点击，
// 展示头部档案与统计摘要，不留空白。
import { Title } from "@solidjs/meta";
import { useParams, useNavigate } from "@solidjs/router";
import { Show, createResource } from "solid-js";
import { fetchPersonProfile } from "~/lib/api/client";
import { Button } from "~/components/ui/button";
import { notify } from "~/components/ui/toast";

export default function PersonDetailPlaceholder() {
  const params = useParams<{ name: string }>();
  const navigate = useNavigate();
  const name = decodeURIComponent(params.name ?? "");
  const [profile] = createResource(() => name, (n) => fetchPersonProfile(n).catch(() => null));

  return (
    <div class="mx-auto flex max-w-4xl flex-col gap-4 px-5 py-8">
      <Title>{name} · 传记工作室</Title>
      <div class="flex items-center gap-3">
        <Button size="sm" variant="ghost" onClick={() => navigate("/")}>
          ← 人物
        </Button>
      </div>

      <div class="rounded-card border border-line bg-surface p-6 shadow-card">
        <div class="flex items-center gap-4">
          <span class="flex h-14 w-14 shrink-0 items-center justify-center rounded-full bg-primary/20 text-2xl font-bold text-ink">
            {name.slice(0, 1)}
          </span>
          <div class="min-w-0">
            <h1 class="text-xl font-bold text-ink">{name}</h1>
            <Show when={profile()} fallback={<p class="mt-1 text-xs text-ink-dim">正在加载档案…</p>}>
              {(p) => (
                <p class="mt-1 truncate text-sm text-ink-dim">
                  {[p().dynasty, p().category].filter(Boolean).join(" · ") || "朝代/类别待补充"}
                  <Show when={(p().key_facts ?? []).length > 0}> · 事实 {p().key_facts.length} 条</Show>
                  <Show when={(p().doc_count ?? 0) > 0}> · 语料 {p().doc_count} 篇</Show>
                </p>
              )}
            </Show>
          </div>
          <div class="ml-auto flex gap-2">
            <Button size="sm" variant="secondary" onClick={() => notify.info("编辑资料", "详情页在里程碑 2 提供")}>
              编辑资料
            </Button>
            <Button size="sm" variant="primary" onClick={() => notify.info("新建生成任务", "创建任务弹窗在里程碑 2 提供")}>
              ＋ 新建生成任务
            </Button>
          </div>
        </div>
      </div>

      <div class="rounded-card border border-dashed border-line bg-surface px-6 py-10 text-center text-sm text-ink-dim">
        文章管理、任务/成片标签、事实时间线、图片素材与右侧进行中任务栏将在<span class="text-ink">里程碑 2</span>交付。
      </div>
    </div>
  );
}
