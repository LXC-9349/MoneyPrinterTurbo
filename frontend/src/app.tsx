import { MetaProvider, Title } from "@solidjs/meta";
import { Router } from "@solidjs/router";
import { FileRoutes } from "@solidjs/start/router";
import { Suspense } from "solid-js";
import { Toaster } from "~/components/ui/toast";
import { TopNav } from "~/components/nav";
import "./app.css";

// 应用根：Router + MetaProvider + 文件路由。Suspense 让流式 SSR 能在异步
// 数据（createAsync/query）解析时先吐出骨架，提升 LCP。Toaster 挂在 root
// 全局唯一，使 notify.* 在任意路由都能渲染到统一的 Toast.Region。
// TopNav 提供工作台/素材库/设置三个顶层区的常驻切换。
export default function App() {
  return (
    <Router
      root={(props) => (
        <MetaProvider>
          <Title>MoneyPrinterTurbo</Title>
          <TopNav />
          <Suspense>{props.children}</Suspense>
          <Toaster />
        </MetaProvider>
      )}
    >
      <FileRoutes />
    </Router>
  );
}
