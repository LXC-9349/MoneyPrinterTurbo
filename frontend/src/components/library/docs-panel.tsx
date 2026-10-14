// 文档检索面板：按姓名检索该人物的文库关键事实与来源文章。
// readPersonDocuments 为服务端代理，按需在点击检索时调用（客户端 RPC）。
import { For, Show, createSignal } from "solid-js";
import { Button } from "~/components/ui/button";
import { Card, CardContent } from "~/components/ui/card";
import { TextField } from "~/components/ui/text-field";
import { notify } from "~/components/ui/toast";
import { readPersonDocuments } from "~/server/queries";
import { ApiError } from "~/lib/api/client";
import type { PersonDocumentsResult } from "~/lib/api/types";

export function DocsPanel() {
  const [name, setName] = createSignal("");
  const [busy, setBusy] = createSignal(false);
  const [result, setResult] = createSignal<PersonDocumentsResult | null>(null);

  const search = async () => {
    const q = name().trim();
    if (!q) {
      notify.error("请输入人物姓名");
      return;
    }
    setBusy(true);
    try {
      setResult(await readPersonDocuments(q));
    } catch (e) {
      setResult(null);
      notify.error("检索失败", e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div class="flex flex-col gap-4">
      <div class="flex flex-wrap items-end gap-3">
        <div class="min-w-48 flex-1">
          <TextField
            label="人物姓名"
            value={name()}
            onInput={setName}
            placeholder="输入姓名以检索其来源文章"
          />
        </div>
        <Button variant="primary" disabled={busy()} onClick={() => void search()}>
          {busy() ? "检索中…" : "检索"}
        </Button>
      </div>

      <Show when={result()}>
        {(r) => (
          <Card>
            <CardContent class="py-4">
              <div class="flex flex-wrap items-baseline gap-2">
                <h3 class="text-base font-bold text-ink">{r().person}</h3>
                <Show when={r().dynasty}>
                  <span class="text-xs text-ink-dim">{r().dynasty}</span>
                </Show>
              </div>

              <Show when={r().key_facts.length > 0}>
                <ul class="mt-2 list-disc pl-5 text-sm text-ink">
                  <For each={r().key_facts}>{(f) => <li>{f}</li>}</For>
                </ul>
              </Show>

              <p class="mt-3 text-xs font-semibold text-ink-dim">
                来源文章（{r().documents.length}）
              </p>
              <Show
                when={r().documents.length > 0}
                fallback={<p class="mt-1 text-sm text-ink-dim">该人物暂无可检索的来源文章。</p>}
              >
                <ul class="mt-2 flex flex-col gap-1.5">
                  <For each={r().documents}>
                    {(d) => (
                      <li class="rounded-control border border-line bg-surface-2 p-2">
                        <div class="flex items-center justify-between gap-2">
                          <span class="text-sm text-ink">{d.title || d.doc_id}</span>
                          <span class="shrink-0 text-xs text-ink-dim">{d.n_chars} 字</span>
                        </div>
                        <div class="mt-0.5 flex gap-2 text-xs text-ink-dim">
                          <Show when={d.category}>
                            <span class="text-primary">{d.category}</span>
                          </Show>
                          <span>{d.doc_id}</span>
                        </div>
                      </li>
                    )}
                  </For>
                </ul>
              </Show>
            </CardContent>
          </Card>
        )}
      </Show>
    </div>
  );
}
