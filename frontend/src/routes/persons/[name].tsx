// 人物传记详情页（/persons/:name，里程碑 2）。
// 头部档案 + 数据条（文章/任务/成片/事实/素材/进行中六项计数）+ 五标签
// （文章/任务/成片/事实/图片素材）+ 右侧「进行中任务」栏。「＋新建生成任务」打开
// CreateTaskDialog，创建走与向导一致的记录路径（见弹窗注释），并刷新本页聚合。
// 读一律走服务端 query（createAsync，SSR 可解析），绝不在 SSR 触发客户端相对 base 取数。
import { Title } from "@solidjs/meta";
import { createAsync, useLocation, useNavigate, useParams } from "@solidjs/router";
import { For, Show, createEffect, createMemo, createSignal } from "solid-js";
import { invalidatePerson, personDetailQuery } from "~/lib/api/data";
import type { PersonAssetItem, PersonFactItem, WorkbenchTaskStatus } from "~/lib/api/types";
import {
  addPersonFact,
  deletePersonFact,
  patchPersonAsset,
  patchPersonFact,
  resolveApiUrl,
  setPersonAssetCover,
  uploadPersonImage,
} from "~/lib/api/client";
import { elapsedSince, relTime, stageLabel } from "~/components/shell";
import { cn } from "~/lib/cn";
import { Button } from "~/components/ui/button";
import { Badge, type BadgeTone } from "~/components/ui/badge";
import { TabPanel, TabsRoot } from "~/components/ui/tabs";
import { ArticlesPanel } from "~/components/person/articles-panel";
import { CreateTaskDialog, type ExistingScript } from "~/components/person/create-task-dialog";
import { notify } from "~/components/ui/toast";

const STATUS_TONE: Record<WorkbenchTaskStatus, BadgeTone> = {
  draft: "neutral",
  queued: "primary",
  processing: "primary",
  awaiting_confirmation: "warn",
  completed: "ok",
  failed: "err",
  cancelled: "neutral",
};
const STATUS_LABEL: Record<WorkbenchTaskStatus, string> = {
  draft: "草稿",
  queued: "排队中",
  processing: "进行中",
  awaiting_confirmation: "待确认",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

function Metric(props: { label: string; value: number; accent?: boolean }) {
  return (
    <div class="flex flex-col items-center rounded-card border border-line bg-surface px-3 py-2">
      <span class={cn("text-lg font-bold tabular-nums", props.accent ? "text-primary" : "text-ink")}>
        {props.value}
      </span>
      <span class="text-[0.7rem] text-ink-dim">{props.label}</span>
    </div>
  );
}

// 素材 path 存相对 storage 根的 posix 路径（tasks/…）；asgi 把 storage/tasks
// 挂在 /tasks 下，因此 tasks/ 前缀路径可直接拼 apiBase 访问。跨盘符等异常
// 路径（绝对盘符）无法经静态挂载服务，退回只显示文件名。
function assetSrc(path: string): string {
  const rel = (path || "").replace(/\\/g, "/");
  if (!rel.startsWith("tasks/")) return "";
  return resolveApiUrl(rel);
}

// 事实面板（里程碑 2 收尾 #4）：facts 表为真源，增删改 + 来源/核验态/人工
// 确认展示；verified=0 的条目提供采纳/修改/忽略三个动作（忽略即删除）。
const FACT_SOURCE_LABEL: Record<string, string> = {
  manual: "手动",
  overlay: "档案迁入",
  import: "库导入",
  llm: "AI 提取",
  bio: "传记管线",
};

function FactsPanel(props: { person: string; facts: PersonFactItem[]; onChanged: () => void }) {
  const [busy, setBusy] = createSignal(false);
  const [draft, setDraft] = createSignal("");
  const [editingId, setEditingId] = createSignal<number | null>(null);
  const [editText, setEditText] = createSignal("");

  const run = async (fn: () => Promise<unknown>, okTitle: string) => {
    if (busy()) return;
    setBusy(true);
    try {
      await fn();
      notify.success(okTitle);
      props.onChanged();
    } catch (e) {
      notify.error("操作失败", e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const submitNew = () => {
    const text = draft().trim();
    if (!text) return;
    void run(() => addPersonFact(props.person, text), "已新增事实").then(() => setDraft(""));
  };

  const saveEdit = (f: PersonFactItem) => {
    const text = editText().trim();
    if (!text) return;
    // 未核验项的“修改”保存即视为人工确认；已核验项的编辑只改正文。
    const patch = f.verified
      ? { statement: text }
      : { statement: text, verified: true, human_confirmed: true };
    void run(() => patchPersonFact(f.fact_id, patch), "已修改事实").then(() =>
      setEditingId(null),
    );
  };

  return (
    <div class="flex flex-col gap-3">
      <div class="flex items-end gap-2">
        <textarea
          class="min-h-[2.25rem] flex-1 rounded-control border border-line bg-surface px-3 py-2 text-sm text-ink outline-none focus:border-primary"
          placeholder="新增一条关键事实（回车提交，Shift+Enter 换行）…"
          value={draft()}
          onInput={(e) => setDraft(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submitNew();
            }
          }}
        />
        <Button size="sm" variant="primary" disabled={busy() || !draft().trim()} onClick={submitNew}>
          ＋ 新增
        </Button>
      </div>

      <Show when={props.facts.length > 0} fallback={
        <p class="py-6 text-center text-sm text-ink-dim">还没有关键事实，在上方新增一条。</p>
      }>
        <ul class="flex flex-col gap-2">
          <For each={props.facts}>
            {(f) => {
              const unverified = () => !f.verified;
              const editing = () => editingId() === f.fact_id;
              return (
                <li class="rounded-card border border-line bg-surface px-3 py-2 text-sm">
                  <Show when={editing()} fallback={
                    <div class="flex items-start justify-between gap-3">
                      <div class="min-w-0">
                        <p class="text-ink" title={f.source_ref || undefined}>{f.statement}</p>
                        <p class="mt-1 flex flex-wrap items-center gap-1.5 text-[0.7rem] text-ink-dim">
                          <Badge tone="neutral" class="!px-1.5 !text-[0.65rem]">
                            {FACT_SOURCE_LABEL[f.source] ?? f.source ?? "未知来源"}
                          </Badge>
                          <Show when={f.verified}>
                            <Badge tone="ok" class="!px-1.5 !text-[0.65rem]">已核验</Badge>
                          </Show>
                          <Show when={unverified()}>
                            <Badge tone="warn" class="!px-1.5 !text-[0.65rem]">未核验</Badge>
                          </Show>
                          <Show when={f.human_confirmed}>
                            <Badge tone="primary" class="!px-1.5 !text-[0.65rem]">人工确认</Badge>
                          </Show>
                          <Show when={f.edited_by_user}>
                            <span>已被你修改</span>
                          </Show>
                          <Show when={f.source_ref}>
                            <span class="truncate" title={f.source_ref}>· {f.source_ref}</span>
                          </Show>
                        </p>
                      </div>
                      <div class="flex shrink-0 gap-1">
                        <Show when={unverified()} fallback={
                          <>
                            <Button size="sm" variant="ghost" disabled={busy()} onClick={() => { setEditingId(f.fact_id); setEditText(f.statement); }}>
                              编辑
                            </Button>
                            <Button size="sm" variant="ghost" disabled={busy()} onClick={() => void run(() => deletePersonFact(f.fact_id), "已删除")}>
                              删除
                            </Button>
                          </>
                        }>
                          {/* 未核验三动作：采纳 / 修改（改正文并确认）/ 忽略（删除） */}
                          <Button size="sm" variant="secondary" disabled={busy()} onClick={() => void run(() => patchPersonFact(f.fact_id, { verified: true, human_confirmed: true }), "已采纳")}>
                            采纳
                          </Button>
                          <Button size="sm" variant="ghost" disabled={busy()} onClick={() => { setEditingId(f.fact_id); setEditText(f.statement); }}>
                            修改
                          </Button>
                          <Button size="sm" variant="ghost" disabled={busy()} onClick={() => void run(() => deletePersonFact(f.fact_id), "已忽略")}>
                            忽略
                          </Button>
                        </Show>
                      </div>
                    </div>
                  }>
                  <div class="flex flex-col gap-2">
                    <textarea
                      class="min-h-[3rem] w-full rounded-control border border-line bg-surface px-2 py-1.5 text-sm text-ink outline-none focus:border-primary"
                      value={editText()}
                      onInput={(e) => setEditText(e.currentTarget.value)}
                    />
                    <div class="flex justify-end gap-1.5">
                      <Button size="sm" variant="ghost" disabled={busy()} onClick={() => setEditingId(null)}>取消</Button>
                      <Button size="sm" variant="primary" disabled={busy() || !editText().trim()} onClick={() => saveEdit(f)}>保存</Button>
                    </div>
                  </div>
                  </Show>
                </li>
              );
            }}
          </For>
        </ul>
      </Show>
    </div>
  );
}

// 图片素材面板（里程碑 2 收尾 #3）：网格展示来源/授权/作者 + 预览，支持
// 设为封面、排除（人物级黑名单，检索侧强制跳过在里程碑 4）/恢复、上传自有图。
function AssetsPanel(props: {
  person: string;
  assets: PersonAssetItem[];
  ancient: boolean;
  onChanged: () => void;
}) {
  const [busy, setBusy] = createSignal(false);
  let fileInput: HTMLInputElement | undefined;

  const run = async (fn: () => Promise<unknown>, okTitle: string) => {
    if (busy()) return;
    setBusy(true);
    try {
      await fn();
      notify.success(okTitle);
      props.onChanged();
    } catch (e) {
      notify.error("操作失败", e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const onPickFile = (file: File | undefined) => {
    if (!file) return;
    void run(() => uploadPersonImage(props.person, file), "已上传并登记图片");
  };

  return (
    <div class="flex flex-col gap-3">
      <Show when={props.ancient}>
        <div class="rounded-card border border-warn/40 bg-warn/10 px-3 py-2 text-xs text-ink">
          此人无真实照片：摄影术（1839）发明之前的人物不存在当代实拍，
          现有图片多为后人绘像或影视取材。可上传你认为合适的画像/剧照作为封面。
        </div>
      </Show>

      <div class="flex items-center justify-between">
        <p class="text-sm text-ink-dim">共 {props.assets.length} 张（含已排除）</p>
        <div class="flex items-center gap-2">
          <input
            ref={fileInput}
            type="file"
            accept="image/jpeg,image/png,image/webp,image/gif,image/bmp"
            class="hidden"
            onChange={(e) => {
              const f = (e.currentTarget as HTMLInputElement).files?.[0];
              onPickFile(f);
              e.currentTarget.value = "";
            }}
          />
          <Button size="sm" variant="secondary" disabled={busy()} onClick={() => fileInput?.click()}>
            ⬆ 上传图片
          </Button>
        </div>
      </div>

      <Show when={props.assets.length > 0} fallback={
        <div class="rounded-card border border-dashed border-line bg-surface px-4 py-10 text-center text-sm text-ink-dim">
          还没有图片素材。流水线登记的任务素材会出现在这里，也可以直接上传图片。
        </div>
      }>
        <ul class="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
          <For each={props.assets}>
            {(a) => {
              const excluded = () => a.state === "excluded";
              const src = () => assetSrc(a.path);
              return (
                <li
                  class={cn(
                    "flex flex-col gap-1.5 rounded-card border border-line bg-surface p-2.5 text-sm",
                    excluded() && "opacity-50",
                  )}
                >
                  <div class="flex items-center gap-2">
                    <Show when={a.is_cover}>
                      <Badge tone="primary" class="!px-2 !text-[0.68rem]">封面</Badge>
                    </Show>
                    <Show when={excluded()}>
                      <Badge tone="neutral" class="!px-2 !text-[0.68rem]">已排除</Badge>
                    </Show>
                    <span class="truncate font-medium text-ink">{(a.path || "").split(/[\\/]/).pop()}</span>
                    <span class="ml-auto shrink-0 text-xs text-ink-dim">{a.kind}</span>
                  </div>
                  <Show when={src()}>
                    <img
                      src={src()}
                      alt={a.source_title || a.path}
                      loading="lazy"
                      class="h-32 w-full rounded-control border border-line object-cover"
                    />
                  </Show>
                  <p class="truncate text-xs text-ink-dim" title={a.source_title || a.source_page}>
                    来源 {a.provider || "未知"}
                    <Show when={a.author}> · 作者 {a.author}</Show>
                    <Show when={a.license}> · {a.license}</Show>
                  </p>
                  <Show when={a.source_page}>
                    <a
                      href={a.source_page}
                      target="_blank"
                      rel="noreferrer"
                      class="truncate text-xs text-primary hover:underline"
                    >
                      来源页 ↗
                    </a>
                  </Show>
                  <div class="mt-auto flex gap-1.5 pt-1">
                    <Show when={!a.is_cover && !excluded()}>
                      <Button size="sm" variant="ghost" disabled={busy()} onClick={() => void run(() => setPersonAssetCover(props.person, a.asset_id), "已设为封面")}>
                        设为封面
                      </Button>
                    </Show>
                    <Show when={!excluded()} fallback={
                      <Button size="sm" variant="ghost" disabled={busy()} onClick={() => void run(() => patchPersonAsset(props.person, a.asset_id, "pending"), "已恢复")}>
                        恢复
                      </Button>
                    }>
                      <Button size="sm" variant="ghost" disabled={busy()} onClick={() => void run(() => patchPersonAsset(props.person, a.asset_id, "excluded"), "已排除（仅对本人）")}>
                        排除
                      </Button>
                    </Show>
                  </div>
                </li>
              );
            }}
          </For>
        </ul>
      </Show>
    </div>
  );
}

export default function PersonDetail() {
  const routeParams = useParams<{ name: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const name = decodeURIComponent(routeParams.name ?? "");

  const detail = createAsync(() => personDetailQuery(name));
  const [dialogOpen, setDialogOpen] = createSignal(false);
  const [tab, setTab] = createSignal("articles");

  // 首页卡片「新建任务」带 ?new=1 进入：自动打开创建弹窗并清掉 query。
  createEffect(() => {
    if (location.query.new === "1") {
      setDialogOpen(true);
      navigate(`/persons/${encodeURIComponent(name)}`, { replace: true });
    }
  });

  const reload = () => invalidatePerson();

  // 可复用脚本版本：从历史任务 params_json 抽取非空 video_script。
  const existingScripts = createMemo<ExistingScript[]>(() => {
    const out: ExistingScript[] = [];
    for (const t of detail()?.tasks ?? []) {
      if (!t.params_json) continue;
      try {
        const p = JSON.parse(t.params_json) as { video_script?: string };
        const script = (p.video_script ?? "").trim();
        if (script)
          out.push({
            task_id: t.task_id,
            label: `${t.title || t.person_name || t.task_id} · ${relTime(t.created_at)}`,
            script,
          });
      } catch {
        /* 脏 params_json 忽略 */
      }
    }
    return out;
  });

  const active = () => detail()?.active_tasks ?? [];

  return (
    <div class="mx-auto flex max-w-[1400px] flex-col gap-4 px-5 py-6 lg:flex-row">
      <div class="flex min-w-0 flex-1 flex-col gap-4">
        <Title>{name} · 传记工作室</Title>

        <div class="flex items-start gap-2">
          <Button size="sm" variant="ghost" onClick={() => navigate("/")}>
            ← 人物
          </Button>
        </div>

        {/* 头部 */}
        <Show when={detail()} fallback={<div class="h-28 animate-pulse rounded-card border border-line bg-surface" />}>
          {(d) => (
            <div class="flex flex-col gap-4 rounded-card border border-line bg-surface p-5 shadow-card">
              <div class="flex items-start gap-4">
                <span class="flex h-14 w-14 shrink-0 items-center justify-center rounded-full bg-primary/20 text-2xl font-bold text-ink">
                  {name.slice(0, 1)}
                </span>
                <div class="min-w-0 flex-1">
                  <div class="flex items-center gap-2">
                    <Show when={d().profile.pinned}>
                      <span title="已置顶" class="text-sm text-warn">📌</span>
                    </Show>
                    <h1 class="text-xl font-bold text-ink">{name}</h1>
                    <Show when={d().profile.source === "custom"}>
                      <Badge tone="primary" class="!px-2 !text-[0.68rem]">自建</Badge>
                    </Show>
                  </div>
                  <p class="mt-1 text-sm text-ink-dim">
                    {[d().profile.dynasty, d().profile.category].filter(Boolean).join(" · ") || "朝代/类别待补充"}
                  </p>
                  <Show when={d().profile.note}>
                    <p class="mt-2 text-sm text-ink">{d().profile.note}</p>
                  </Show>
                </div>
                <div class="flex shrink-0 gap-2">
                  <Button size="sm" variant="secondary" onClick={() => notify.info("编辑资料", "档案编辑在后续里程碑完善")}>
                    编辑资料
                  </Button>
                  <Button size="sm" variant="primary" onClick={() => setDialogOpen(true)}>
                    ＋ 新建生成任务
                  </Button>
                </div>
              </div>

              <div class="grid grid-cols-3 gap-2 sm:grid-cols-6">
                <Metric label="文章" value={d().summary.article_count} />
                <Metric label="任务" value={d().summary.task_count} />
                <Metric label="成片" value={d().summary.video_count} />
                <Metric label="事实" value={d().summary.fact_count} />
                <Metric label="图片素材" value={d().summary.asset_count} />
                <Metric label="进行中" value={d().summary.active_count} accent={d().summary.active_count > 0} />
              </div>
            </div>
          )}
        </Show>

        <Show when={detail()}>
          {(d) => (
            <TabsRoot
              class="mt-2"
              value={tab()}
              onChange={setTab}
              items={[
                { value: "articles", label: `文章 ${d().summary.article_count}` },
                { value: "tasks", label: `任务 ${d().summary.task_count}` },
                { value: "videos", label: `成片 ${d().summary.video_count}` },
                { value: "facts", label: `事实 ${d().summary.fact_count}` },
                { value: "assets", label: `图片素材 ${d().summary.asset_count}` },
              ]}
            >
              <TabPanel value="articles" class="pt-4">
                <ArticlesPanel person={name} />
              </TabPanel>

              <TabPanel value="tasks" class="pt-4">
                <Show when={d().tasks.length > 0} fallback={
                  <div class="rounded-card border border-dashed border-line bg-surface px-4 py-10 text-center text-sm text-ink-dim">
                    还没有任务。
                    <button type="button" class="ml-1 font-semibold text-primary" onClick={() => setDialogOpen(true)}>
                      新建生成任务 →
                    </button>
                  </div>
                }>
                  <ul class="flex flex-col gap-2">
                    <For each={d().tasks}>
                      {(t) => (
                        <li class="flex items-center justify-between gap-3 rounded-card border border-line bg-surface px-3 py-2">
                          <div class="min-w-0">
                            <p class="truncate text-sm font-medium text-ink">
                              {t.title || t.person_name || t.task_id}
                              <Show when={t.version > 1}><span class="ml-1 text-xs text-ink-dim">v{t.version}</span></Show>
                            </p>
                            <p class="mt-0.5 text-xs text-ink-dim">
                              {relTime(t.created_at)}
                              <Show when={t.status === "processing" || t.status === "queued"}>
                                {" · "}{stageLabel(t.stage)} · {t.progress}%
                              </Show>
                              <Show when={t.status === "failed" && t.error}>
                                {" · "}<span class="text-err">{t.error.slice(0, 40)}</span>
                              </Show>
                            </p>
                          </div>
                          <div class="flex shrink-0 items-center gap-2">
                            <Badge tone={STATUS_TONE[t.status] ?? "neutral"}>{STATUS_LABEL[t.status] ?? t.status}</Badge>
                            <Button size="sm" variant="ghost" onClick={() => navigate(`/tasks/${t.task_id}`)}>详情</Button>
                          </div>
                        </li>
                      )}
                    </For>
                  </ul>
                </Show>
              </TabPanel>

              <TabPanel value="videos" class="pt-4">
                <Show when={d().videos.length > 0} fallback={<p class="py-6 text-center text-sm text-ink-dim">还没有成片。</p>}>
                  <ul class="flex flex-col gap-2">
                    <For each={d().videos}>
                      {(v) => (
                        <li class="flex items-center justify-between gap-3 rounded-card border border-line bg-surface px-3 py-2 text-sm">
                          <div class="min-w-0">
                            <p class="truncate font-medium text-ink">{v.path.split(/[\\/]/).pop()}</p>
                            <p class="mt-0.5 truncate text-xs text-ink-dim" title={v.path}>{v.path}</p>
                          </div>
                          <div class="flex shrink-0 items-center gap-2">
                            <span class="text-xs text-ink-dim">{relTime(v.created_at)}</span>
                            <Button size="sm" variant="ghost" onClick={() => navigate(`/tasks/${v.task_id}`)}>查看任务</Button>
                          </div>
                        </li>
                      )}
                    </For>
                  </ul>
                </Show>
              </TabPanel>

              <TabPanel value="facts" class="pt-4">
                <FactsPanel person={name} facts={d().facts} onChanged={reload} />
              </TabPanel>

              <TabPanel value="assets" class="pt-4">
                <AssetsPanel person={name} assets={d().assets} ancient={!!d().no_authentic_photo} onChanged={reload} />
              </TabPanel>
            </TabsRoot>
          )}
        </Show>
      </div>

      {/* 右：进行中任务栏 */}
      <aside class="w-full shrink-0 lg:w-72">
        <div class="sticky top-16 flex flex-col gap-3 rounded-card border border-line bg-surface p-3 shadow-card">
          <div class="flex items-center justify-between">
            <h3 class="text-sm font-bold text-ink">进行中任务</h3>
            <Show when={active().length > 0}>
              <span class="animate-pulse rounded-full bg-primary/25 px-2 text-xs font-bold text-primary">{active().length}</span>
            </Show>
          </div>
          <Show when={active().length > 0} fallback={<p class="py-4 text-center text-xs text-ink-dim">当前没有进行中的任务。</p>}>
            <For each={active()}>
              {(t) => (
                <div class="rounded-control border border-line bg-surface-2 p-2.5">
                  <p class="truncate text-sm font-medium text-ink">{t.title || t.task_id}</p>
                  <p class="mt-0.5 text-xs text-ink-dim">
                    {stageLabel(t.stage) || t.status} · {t.progress}% · {elapsedSince(t.started_at || t.created_at)}
                  </p>
                  <div class="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-surface">
                    <div class={cn("h-full rounded-full", t.progress >= 100 ? "bg-ok" : "bg-primary")} style={{ width: `${Math.min(100, t.progress)}%` }} />
                  </div>
                  <button type="button" class="mt-1.5 text-xs text-primary hover:underline" onClick={() => navigate(`/tasks/${t.task_id}`)}>
                    查看详情 →
                  </button>
                </div>
              )}
            </For>
          </Show>
        </div>
      </aside>

      {/* 仅打开时挂载：弹窗内的音色/BGM/素材选项为客户端按需加载，不参与 SSR。 */}
      <Show when={dialogOpen()}>
        <CreateTaskDialog
          person={name}
          open
          onClose={() => setDialogOpen(false)}
          existingScripts={existingScripts()}
          onCreated={() => reload()}
        />
      </Show>
    </div>
  );
}
