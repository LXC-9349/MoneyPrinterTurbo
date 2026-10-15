// 里程碑 2 · 人物详情页「文章」标签：文章管理。
// 三种添加方式（粘贴 / 上传 txt·md / 从史料库检索添加）+ 每篇的「用于生成」开关、
// 权重、「低可信来源」标注。另展示史料库软引用行（#6）：开关/权重存 library_links，
// 低可信默认不启用，可一键添加为正式文章。
// 读走服务端 query（SSR 可解析，绝不触发客户端相对 base 的 SSR 崩溃）；写走客户端直连
// article CRUD，成功后 revalidate 本页聚合（详情 + 文章），父级摘要计数随之更新。
import { For, Show, createMemo, createSignal } from "solid-js";
import { createAsync } from "@solidjs/router";
import { articlesQuery, invalidatePerson, libraryArticlesQuery } from "~/lib/api/data";
import { addArticle, deleteArticle, patchArticle, patchPersonLibraryLink } from "~/lib/api/client";
import type { Article, LibraryArticleDoc, PersonLibraryArticleItem } from "~/lib/api/types";
import { cn } from "~/lib/cn";
import { Button } from "~/components/ui/button";
import { Badge } from "~/components/ui/badge";
import { KSwitch } from "~/components/ui/switch";
import { KSlider } from "~/components/ui/slider";
import { TextArea } from "~/components/ui/text-area";
import { TextField } from "~/components/ui/text-field";
import { notify } from "~/components/ui/toast";

type AddMode = "none" | "paste" | "library";

const SOURCE_LABEL: Record<string, string> = {
  paste: "粘贴",
  upload: "上传",
  library: "史料库",
};

export function ArticlesPanel(props: { person: string; soft?: PersonLibraryArticleItem[] }) {
  const feed = createAsync(() => articlesQuery(props.person));
  const [ovEnabled, setOvEnabled] = createSignal<Record<number, boolean>>({});
  const [ovWeight, setOvWeight] = createSignal<Record<number, number>>({});
  // 软引用行的乐观覆盖（按 doc_id）：PATCH 在途时先展示目标态，失败回滚。
  const [ovSoftEnabled, setOvSoftEnabled] = createSignal<Record<string, boolean>>({});
  const [ovSoftWeight, setOvSoftWeight] = createSignal<Record<string, number>>({});

  const articles = createMemo<Article[]>(() =>
    (feed() ?? []).map((a) => {
      const ov = ovEnabled()[a.article_id];
      return {
        ...a,
        enabled: ov == null ? a.enabled : ov ? 1 : 0,
        weight: ovWeight()[a.article_id] ?? a.weight,
      };
    }),
  );
  const enabledCount = createMemo(() => articles().filter((a) => a.enabled).length);

  const softRows = createMemo<PersonLibraryArticleItem[]>(() =>
    (props.soft ?? []).map((d) => {
      const ov = ovSoftEnabled()[d.doc_id];
      return {
        ...d,
        enabled: ov == null ? d.enabled : ov,
        weight: ovSoftWeight()[d.doc_id] ?? d.weight,
      };
    }),
  );
  const softEnabledCount = createMemo(() => softRows().filter((d) => d.enabled).length);

  const [mode, setMode] = createSignal<AddMode>("none");
  const [pTitle, setPTitle] = createSignal("");
  const [pBody, setPBody] = createSignal("");

  // 史料库文档：点开时才拉（客户端动作），不进 SSR。
  const [libDocs, setLibDocs] = createSignal<LibraryArticleDoc[]>([]);
  const [libLoading, setLibLoading] = createSignal(false);
  const [libLoaded, setLibLoaded] = createSignal(false);

  const openLibrary = async () => {
    if (libLoaded()) return;
    setLibLoading(true);
    try {
      const r = await libraryArticlesQuery(props.person);
      setLibDocs(r.documents);
      setLibLoaded(true);
    } catch (e) {
      notify.error("史料库检索失败", e instanceof Error ? e.message : String(e));
    } finally {
      setLibLoading(false);
    }
  };

  const toggleMode = (m: AddMode) => {
    const next = mode() === m ? "none" : m;
    setMode(next);
    if (next === "library") void openLibrary();
  };

  const submitPaste = async () => {
    const body = pBody().trim();
    if (!body) {
      notify.info("内容为空", "请先粘贴或上传文章内容。");
      return;
    }
    try {
      await addArticle(props.person, { source: "paste", title: pTitle().trim() || undefined, content: body });
      notify.success("已添加文章", pTitle().trim() || body.slice(0, 16));
      setPTitle("");
      setPBody("");
      setMode("none");
      invalidatePerson();
    } catch (e) {
      notify.error("添加失败", e instanceof Error ? e.message : String(e));
    }
  };

  const onUpload = (e: Event) => {
    const input = e.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = async () => {
      const text = String(reader.result ?? "");
      try {
        await addArticle(props.person, {
          source: "upload",
          title: file.name.replace(/\.(txt|md)$/i, ""),
          content: text,
        });
        notify.success("已上传文章", file.name);
        setMode("none");
        invalidatePerson();
      } catch (err) {
        notify.error("上传失败", err instanceof Error ? err.message : String(err));
      }
    };
    reader.readAsText(file);
    input.value = "";
  };

  const addFromLibrary = async (d: LibraryArticleDoc) => {
    try {
      await addArticle(props.person, {
        source: "library",
        doc_id: d.doc_id,
        title: d.title,
        excerpt: d.excerpt,
        reliability: d.reliability,
      });
      notify.success("已从史料库添加", d.title || d.doc_id);
      invalidatePerson();
    } catch (e) {
      notify.error("添加失败", e instanceof Error ? e.message : String(e));
    }
  };

  const setEnabled = async (a: Article, v: boolean) => {
    setOvEnabled((prev) => ({ ...prev, [a.article_id]: v }));
    try {
      await patchArticle(a.article_id, { enabled: v });
    } catch (e) {
      setOvEnabled((prev) => {
        const n = { ...prev };
        delete n[a.article_id];
        return n;
      });
      notify.error("更新失败", e instanceof Error ? e.message : String(e));
      return;
    }
    invalidatePerson();
    setOvEnabled((prev) => {
      const n = { ...prev };
      delete n[a.article_id];
      return n;
    });
  };

  const setWeight = async (a: Article, v: number) => {
    setOvWeight((prev) => ({ ...prev, [a.article_id]: v }));
    try {
      await patchArticle(a.article_id, { weight: v });
    } catch (e) {
      setOvWeight((prev) => {
        const n = { ...prev };
        delete n[a.article_id];
        return n;
      });
      notify.error("权重更新失败", e instanceof Error ? e.message : String(e));
    }
  };

  const remove = async (a: Article) => {
    if (!window.confirm(`删除文章「${a.title}」？`)) return;
    try {
      await deleteArticle(a.article_id);
      notify.success("已删除", a.title);
      invalidatePerson();
    } catch (e) {
      notify.error("删除失败", e instanceof Error ? e.message : String(e));
    }
  };

  // 软引用行开关/权重：状态存 library_links（人物维度），成功后 revalidate 详情。
  const setSoftEnabled = async (d: PersonLibraryArticleItem, v: boolean) => {
    setOvSoftEnabled((prev) => ({ ...prev, [d.doc_id]: v }));
    try {
      await patchPersonLibraryLink(props.person, d.doc_id, { enabled: v });
    } catch (e) {
      setOvSoftEnabled((prev) => {
        const n = { ...prev };
        delete n[d.doc_id];
        return n;
      });
      notify.error("更新失败", e instanceof Error ? e.message : String(e));
      return;
    }
    invalidatePerson();
    setOvSoftEnabled((prev) => {
      const n = { ...prev };
      delete n[d.doc_id];
      return n;
    });
  };

  const setSoftWeight = async (d: PersonLibraryArticleItem, v: number) => {
    setOvSoftWeight((prev) => ({ ...prev, [d.doc_id]: v }));
    try {
      await patchPersonLibraryLink(props.person, d.doc_id, { weight: v });
    } catch (e) {
      setOvSoftWeight((prev) => {
        const n = { ...prev };
        delete n[d.doc_id];
        return n;
      });
      notify.error("权重更新失败", e instanceof Error ? e.message : String(e));
    }
  };

  // 提升为正式文章：拷入 articles 表后从软引用列表自动消失（不双计）。
  const promoteSoft = async (d: PersonLibraryArticleItem) => {
    try {
      await addArticle(props.person, {
        source: "library",
        doc_id: d.doc_id,
        title: d.title,
        reliability: d.reliability,
      });
      notify.success("已添加为正式文章", d.title || d.doc_id);
      invalidatePerson();
    } catch (e) {
      notify.error("添加失败", e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div class="flex flex-col gap-3">
      <div class="flex flex-wrap items-center gap-2">
        <Button size="sm" variant={mode() === "paste" ? "primary" : "secondary"} onClick={() => toggleMode("paste")}>
          ＋ 粘贴 / 上传
        </Button>
        <Button size="sm" variant={mode() === "library" ? "primary" : "secondary"} onClick={() => toggleMode("library")}>
          从史料库添加
        </Button>
        <span class="ml-auto text-xs text-ink-dim">
          共 {articles().length + softRows().length} 篇 · 启用 {enabledCount() + softEnabledCount()} 篇
          <Show when={softRows().length > 0}>
            （含史料库 {softRows().length} 篇）
          </Show>
        </span>
      </div>

      <Show when={mode() === "paste"}>
        <div class="flex flex-col gap-2 rounded-card border border-line bg-surface-2 p-3">
          <TextField label="标题（可留空，取正文首行）" value={pTitle()} onInput={setPTitle} placeholder="如：早年生平" />
          <TextArea id="paste-body" label="正文" value={pBody()} onInput={setPBody} rows={5} placeholder="粘贴文章正文…" />
          <div class="flex items-center gap-2">
            <label class="cursor-pointer rounded-control border border-line bg-surface px-2.5 py-1 text-xs text-ink hover:border-line-strong">
              上传 .txt / .md
              <input type="file" accept=".txt,.md,text/plain,text/markdown" class="hidden" onChange={onUpload} />
            </label>
            <Button size="sm" variant="primary" class="ml-auto" onClick={() => void submitPaste()}>
              保存文章
            </Button>
          </div>
        </div>
      </Show>

      <Show when={mode() === "library"}>
        <div class="flex flex-col gap-2 rounded-card border border-line bg-surface-2 p-3">
          <Show when={libLoading()} fallback={
            <Show when={libDocs().length > 0} fallback={<p class="text-xs text-ink-dim">该人物在史料库中没有可添加的语料。</p>}>
              <For each={libDocs()}>
                {(d) => (
                  <div class="flex items-start justify-between gap-2 rounded-control border border-line bg-surface px-3 py-2">
                    <div class="min-w-0">
                      <p class="truncate text-sm font-medium text-ink">
                        {d.title || d.doc_id}
                        <Show when={d.category}><span class="ml-2 text-xs text-ink-dim">{d.category}</span></Show>
                        <Show when={d.reliability === "low"}>
                          <Badge tone="warn" class="ml-2 !px-2 !text-[0.68rem]">低可信</Badge>
                        </Show>
                      </p>
                      <p class="mt-0.5 line-clamp-2 text-xs text-ink-dim">{d.excerpt || "（无摘要）"} · {d.n_chars} 字</p>
                    </div>
                    <Button size="sm" variant="secondary" onClick={() => void addFromLibrary(d)}>添加</Button>
                  </div>
                )}
              </For>
            </Show>
          }>
            <p class="text-xs text-ink-dim">检索史料库语料中…</p>
          </Show>
        </div>
      </Show>

      <Show when={feed()} fallback={
        <div class="flex flex-col gap-2">
          <For each={Array.from({ length: 2 })}>
            {() => <div class="h-20 animate-pulse rounded-card border border-line bg-surface" />}
          </For>
        </div>
      }>
        <For each={articles()}>
          {(a) => {
            const low = () => a.reliability === "low";
            return (
              <div class={cn("rounded-card border bg-surface p-3", a.enabled ? "border-line" : "border-line/60 opacity-90")}>
                <div class="flex items-start justify-between gap-3">
                  <div class="min-w-0 flex-1">
                    <div class="flex items-center gap-1.5">
                      <h4 class="truncate text-sm font-semibold text-ink">{a.title}</h4>
                      <Badge tone={a.source === "library" ? "primary" : "neutral"} class="!px-2 !text-[0.68rem]">
                        {SOURCE_LABEL[a.source] ?? a.source}
                      </Badge>
                      <Show when={low()}>
                        <Badge tone="warn" class="!px-2 !text-[0.68rem]">低可信来源</Badge>
                      </Show>
                    </div>
                    <p class="mt-1 line-clamp-2 text-xs text-ink-dim">{a.excerpt || "（无摘要）"}</p>
                    <p class="mt-1 text-[0.7rem] text-ink-dim">{a.word_count} 字</p>
                  </div>
                  <div class="flex shrink-0 flex-col items-end gap-2">
                    <KSwitch checked={!!a.enabled} onChange={(v) => void setEnabled(a, v)} label={<span class="text-xs">用于生成</span>} />
                    <button type="button" class="text-xs text-ink-dim hover:text-err" onClick={() => void remove(a)}>删除</button>
                  </div>
                </div>
                <div class="mt-2 max-w-xs">
                  <KSlider
                    label={<span class="text-xs">权重</span>}
                    value={a.weight}
                    minValue={0}
                    maxValue={3}
                    step={0.1}
                    formatValue={(n) => n.toFixed(1)}
                    onChange={(v) => void setWeight(a, v)}
                  />
                </div>
                <Show when={low() && a.enabled}>
                  <p class="mt-1 text-[0.7rem] text-warn">该来源可信度低，已手动启用，请谨慎用于生成。</p>
                </Show>
              </div>
            );
          }}
        </For>
      </Show>

      {/* 史料库软引用行（#6）：不拷贝正文，开关/权重存 library_links；低可信默认不启用并标注。 */}
      <Show when={softRows().length > 0}>
        <div class="flex flex-col gap-2">
          <p class="text-xs font-semibold text-ink-dim">
            史料库关联文章 · {softRows().length} 篇（启用 {softEnabledCount()} 篇）
          </p>
          <For each={softRows()}>
            {(d) => {
              const low = () => d.reliability === "low";
              return (
                <div class={cn("rounded-card border bg-surface p-3", d.enabled ? "border-line" : "border-line/60 opacity-90")}>
                  <div class="flex items-start justify-between gap-3">
                    <div class="min-w-0 flex-1">
                      <div class="flex items-center gap-1.5">
                        <h4 class="truncate text-sm font-semibold text-ink">{d.title || d.doc_id}</h4>
                        <Badge tone="primary" class="!px-2 !text-[0.68rem]">史料库</Badge>
                        <Show when={d.category}><span class="text-xs text-ink-dim">{d.category}</span></Show>
                        <Show when={low()}>
                          <Badge tone="warn" class="!px-2 !text-[0.68rem]">低可信来源</Badge>
                        </Show>
                      </div>
                      <p class="mt-1 text-[0.7rem] text-ink-dim">{d.n_chars} 字 · 未拷贝正文，生成时按 doc_id 实时取用</p>
                    </div>
                    <div class="flex shrink-0 flex-col items-end gap-2">
                      <KSwitch checked={d.enabled} onChange={(v) => void setSoftEnabled(d, v)} label={<span class="text-xs">用于生成</span>} />
                      <Button size="sm" variant="secondary" onClick={() => void promoteSoft(d)}>添加为正式文章</Button>
                    </div>
                  </div>
                  <div class="mt-2 max-w-xs">
                    <KSlider
                      label={<span class="text-xs">权重</span>}
                      value={d.weight}
                      minValue={0}
                      maxValue={3}
                      step={0.1}
                      formatValue={(n) => n.toFixed(1)}
                      onChange={(v) => void setSoftWeight(d, v)}
                    />
                  </div>
                  <Show when={low() && d.enabled}>
                    <p class="mt-1 text-[0.7rem] text-warn">该来源可信度低，已手动启用，请谨慎用于生成。</p>
                  </Show>
                </div>
              );
            }}
          </For>
        </div>
      </Show>

      <Show when={feed() && articles().length === 0 && softRows().length === 0}>
        <div class="rounded-card border border-dashed border-line bg-surface px-4 py-8 text-center text-sm text-ink-dim">
          还没有文章。粘贴一段史料、上传 txt/md，或从史料库检索添加，作为传记文案的事实来源。
        </div>
      </Show>
    </div>
  );
}
