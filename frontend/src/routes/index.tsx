import { Title } from "@solidjs/meta";
import { useNavigate } from "@solidjs/router";
import { onMount } from "solid-js";

// 根路径直接落到工作台（项目列表）。SSR 阶段渲染一个占位，客户端挂载后立即跳转。
export default function Home() {
  const navigate = useNavigate();
  onMount(() => navigate("/projects", { replace: true }));
  return (
    <main class="mx-auto max-w-3xl px-6 py-10">
      <Title>MoneyPrinterTurbo · 工作台</Title>
      <p class="text-sm text-ink-dim">正在进入工作台…</p>
    </main>
  );
}
