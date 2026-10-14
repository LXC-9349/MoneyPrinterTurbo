// 素材库页：语料统计 + 「文档检索 / 人物管理」两个子标签。
// 统计走 libraryCountsQuery（SSR 预取）；两个面板用 lazy() 做面板级 code-splitting，
// 只在切换到对应标签时才拉起其 JS（人物管理面板较重）。
import { lazy, Show } from "solid-js";
import { Title } from "@solidjs/meta";
import { createAsync } from "@solidjs/router";
import { TabsRoot, TabPanel } from "~/components/ui/tabs";
import { libraryCountsQuery } from "~/lib/api/data";

const DocsPanel = lazy(() =>
  import("~/components/library/docs-panel").then((m) => ({ default: m.DocsPanel })),
);
const PersonsPanel = lazy(() =>
  import("~/components/library/persons-panel").then((m) => ({ default: m.PersonsPanel })),
);

function Metric(props: { label: string; value: number | null | undefined }) {
  return (
    <div class="rounded-card border border-line bg-surface px-4 py-3 shadow-card">
      <p class="text-xs text-ink-dim">{props.label}</p>
      <p class="mt-1 text-xl font-bold text-ink">
        {props.value == null ? "—" : props.value.toLocaleString()}
      </p>
    </div>
  );
}

export default function Library() {
  const counts = createAsync(() => libraryCountsQuery());

  return (
    <main class="mx-auto max-w-5xl px-6 py-8">
      <Title>素材库 · MoneyPrinterTurbo</Title>
      <header class="mb-6">
        <h1 class="text-2xl font-semibold text-ink">素材库</h1>
        <p class="mt-1 text-sm text-ink-dim">历史档案语料的检索与人物档案管理。</p>
      </header>

      <Show when={counts()} fallback={null}>
        {(c) => (
          <div class="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Metric label="文档" value={c().documents} />
            <Metric label="书籍" value={c().books} />
            <Metric label="章节" value={c().chapters} />
            <Metric label="文库人物" value={c().persons} />
          </div>
        )}
      </Show>

      <TabsRoot
        class="mt-2"
        items={[
          { value: "persons", label: "人物管理" },
          { value: "docs", label: "文档检索" },
        ]}
      >
        <TabPanel value="persons" class="pt-4">
          <PersonsPanel />
        </TabPanel>
        <TabPanel value="docs" class="pt-4">
          <DocsPanel />
        </TabPanel>
      </TabsRoot>
    </main>
  );
}
