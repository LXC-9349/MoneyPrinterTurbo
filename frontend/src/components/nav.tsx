// 全局顶部导航：工作台（/projects）、素材库（/library）、设置（/settings）。
// 用 Solid <A> 的 activeClass 做选中态高亮，零运行时依赖。放在 app 根布局里，
// 所有业务页共享；向导页在其内容区自带步骤条，这里只提供顶层区切换。
import { A } from "@solidjs/router";
import { For } from "solid-js";

const LINKS = [
  { href: "/projects", label: "工作台" },
  { href: "/library", label: "素材库" },
  { href: "/settings", label: "设置" },
] as const;

export function TopNav() {
  return (
    <nav class="sticky top-0 z-30 border-b border-line bg-surface/90 backdrop-blur">
      <div class="mx-auto flex max-w-5xl items-center gap-4 px-6 py-2.5">
        <span class="text-sm font-bold text-ink">🎬 MoneyPrinterTurbo</span>
        <div class="flex gap-1">
          <For each={LINKS}>
            {(l) => (
              <A
                href={l.href}
                class="rounded-control px-3 py-1 text-sm font-medium text-ink-dim transition-colors hover:text-ink"
                activeClass="bg-primary/15 text-ink"
                inactiveClass=""
              >
                {l.label}
              </A>
            )}
          </For>
        </div>
      </div>
    </nav>
  );
}
