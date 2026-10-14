// 设置页：「全局配置」与「存储/缓存」两个子标签。
// 两个面板都含运维/私密信息，一律客户端渲染（面板内部 onMount 直连 FastAPI），
// 不做 SSR 预取以免 secret 进入首屏 HTML；用 lazy() 做面板级 code-splitting。
import { lazy } from "solid-js";
import { Title } from "@solidjs/meta";
import { TabsRoot, TabPanel } from "~/components/ui/tabs";

const ConfigPanel = lazy(() =>
  import("~/components/settings/config-panel").then((m) => ({ default: m.SettingsConfigPanel })),
);
const StorageCachePanel = lazy(() =>
  import("~/components/settings/storage-panel").then((m) => ({ default: m.StoragePanel })),
);

export default function Settings() {
  return (
    <main class="mx-auto max-w-3xl px-6 py-8">
      <Title>设置 · MoneyPrinterTurbo</Title>
      <header class="mb-6">
        <h1 class="text-2xl font-semibold text-ink">设置</h1>
        <p class="mt-1 text-sm text-ink-dim">全局配置与本地存储管理。</p>
      </header>

      <TabsRoot
        items={[
          { value: "config", label: "全局配置" },
          { value: "storage", label: "存储与缓存" },
        ]}
      >
        <TabPanel value="config" class="pt-4">
          <ConfigPanel />
        </TabPanel>
        <TabPanel value="storage" class="pt-4">
          <StorageCachePanel />
        </TabPanel>
      </TabsRoot>
    </main>
  );
}
