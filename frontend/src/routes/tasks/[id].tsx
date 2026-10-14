// 任务详情占位页（/tasks/:id）。里程碑 3 交付完整详情（阶段时间线、分镜与素材、
// 日志、取消/重试/续跑、调优版本化）。本页先保证深链不 404，并展示基础元信息。
import { Title } from "@solidjs/meta";
import { useParams, useNavigate, createAsync } from "@solidjs/router";
import { For, Show } from "solid-js";
import { wbTasksQuery } from "~/lib/api/data";
import { relTime, stageLabel } from "~/components/shell";
import { Badge } from "~/components/ui/badge";
import { Button } from "~/components/ui/button";

export default function TaskDetailPlaceholder() {
  const params = useParams<{ id: string }>();
  const navigate = useNavigate();
  // 复用列表 query 后前端过滤（limit 500 覆盖当前规模）；正式版换单任务端点。
  const data = createAsync(() => wbTasksQuery({ limit: 500 }));
  const task = createAsync(async () => (await data())?.tasks.find((t) => t.task_id === params.id));

  return (
    <div class="mx-auto flex max-w-3xl flex-col gap-4 px-5 py-8">
      <Title>任务详情 · 传记工作室</Title>
      <div class="flex items-center gap-3">
        <Button size="sm" variant="ghost" onClick={() => navigate("/tasks")}>
          ← 任务列表
        </Button>
        <h1 class="text-lg font-bold text-ink">任务详情</h1>
      </div>
      <Show when={data()} fallback={<p class="text-sm text-ink-dim">正在加载…</p>}>
        <Show
          when={task()}
          fallback={
            <p class="rounded-card border border-line bg-surface p-4 text-sm text-ink-dim">
              未找到任务 <code class="text-ink">{params.id}</code>（可能尚未同步或已被清理）。
            </p>
          }
        >
          {(t) => (
            <div class="flex flex-col gap-3 rounded-card border border-line bg-surface p-5 shadow-card">
              <div class="flex items-center justify-between gap-3">
                <h2 class="text-base font-bold text-ink">{t().title || t().person_name || t().task_id}</h2>
                <Badge tone={t().status === "completed" ? "ok" : t().status === "failed" ? "err" : "primary"}>
                  {t().status}
                </Badge>
              </div>
              <dl class="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
                <Row k="task_id" v={t().task_id} />
                <Row k="人物" v={t().person_name || "—"} />
                <Row k="阶段" v={stageLabel(t().stage) || t().stage || "—"} />
                <Row k="进度" v={`${t().progress}%`} />
                <Row k="创建" v={relTime(t().created_at)} />
                <Row k="更新" v={relTime(t().updated_at)} />
                <Show when={t().error}>
                  <Row k="错误" v={t().error} />
                </Show>
              </dl>
              <Show when={t().warnings_json}>
                {(w) => {
                  let parsed: unknown[] = [];
                  try {
                    parsed = JSON.parse(w()) as unknown[];
                  } catch {
                    /* 忽略非法 JSON */
                  }
                  return (
                    <Show when={parsed.length > 0}>
                      <div class="border-t border-line pt-3">
                        <p class="mb-1 text-xs font-semibold text-warn">提醒（{parsed.length}）</p>
                        <ul class="list-disc pl-5 text-xs text-ink-dim">
                          <For each={parsed}>{(x) => <li>{String(x)}</li>}</For>
                        </ul>
                      </div>
                    </Show>
                  );
                }}
              </Show>
              <p class="border-t border-line pt-3 text-xs text-ink-dim">
                阶段时间线、分镜与素材、日志、重试/续跑与调优版本化将在里程碑 3 提供。
              </p>
            </div>
          )}
        </Show>
      </Show>
    </div>
  );
}

function Row(props: { k: string; v: string }) {
  return (
    <>
      <dt class="text-ink-dim">{props.k}</dt>
      <dd class="min-w-0 truncate text-ink" title={props.v}>
        {props.v}
      </dd>
    </>
  );
}
