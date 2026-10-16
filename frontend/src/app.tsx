import { MetaProvider, Title } from "@solidjs/meta";
import { Router } from "@solidjs/router";
import { FileRoutes } from "@solidjs/start/router";
import { Suspense } from "solid-js";
import { Toaster } from "~/components/ui/toast";
import { AppShell } from "~/components/shell";
import "./app.css";

// 应用根：Router + MetaProvider + 文件路由。Suspense 让流式 SSR 能在异步
// 数据（createAsync/query）解析时先吐出骨架，提升 LCP。Toaster 挂在 root
// 全局唯一，使 notify.* 在任意路由都能渲染到统一的 Toast.Region。
// AppShell（§一）：左侧导航（人物/任务/史料库/设置）+ 顶栏（全局搜索、
// 进行中任务抽屉、系统状态灯），取代旧 TopNav；旧向导路由不再进导航。
export default function App() {
  return (
    <Router
      root={(props) => (
        <MetaProvider>
          <Title>传记工作室 · 人物工作台</Title>
          <AppShell>
            <Suspense>{props.children}</Suspense>
          </AppShell>
          <Toaster />
        </MetaProvider>
      )}
    >
      <FileRoutes />
    </Router>
  );
}
