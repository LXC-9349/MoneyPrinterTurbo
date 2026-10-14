// /projects/{id}/{step} → 载入项目详情（query，SSR 预取含 draft），用
// WorkspaceProvider 承载 draft/轮询状态，渲染 WizardShell。列表页→本项目会全新
// 挂载本路由（store 从 entry.draft 初始化）；同一项目内在四步间导航时，本组件保持
// 挂载（仅 :step 变），store 不重建、编辑态不丢。非法 step 回退到 content。
import { Show, createEffect } from "solid-js";
import { Title } from "@solidjs/meta";
import { A, createAsync, useNavigate, useParams } from "@solidjs/router";
import { projectQuery } from "~/lib/api/data";
import { WORKFLOW_STEPS, type WorkflowStep } from "~/lib/api/types";
import { WorkspaceProvider } from "~/lib/stores/workspace";
import { WizardShell } from "~/components/workspace/wizard-shell";

export default function ProjectStep() {
  const routeParams = useParams();
  const navigate = useNavigate();
  const id = routeParams.id!;
  const entry = createAsync(() => projectQuery(id));

  const step = (): WorkflowStep =>
    WORKFLOW_STEPS.includes(routeParams.step as WorkflowStep)
      ? (routeParams.step as WorkflowStep)
      : "content";

  // 非法 step：解析后规范化到 content（保留已解锁路径由 shell 处理）。
  createEffect(() => {
    if (entry() && !WORKFLOW_STEPS.includes(routeParams.step as WorkflowStep)) {
      navigate(`/projects/${id}/${step()}`, { replace: true });
    }
  });

  return (
    <Show
      when={entry()}
      fallback={
        <main class="mx-auto max-w-5xl px-6 py-10">
          <Title>载入项目 · MoneyPrinterTurbo</Title>
          <p class="text-sm text-ink-dim">正在载入项目…</p>
        </main>
      }
    >
      {(e) => (
        <main class="mx-auto max-w-6xl px-6 py-8">
          <Title>
            {e().title || (e().mode === "biography" ? "传记项目" : "项目")} · MoneyPrinterTurbo
          </Title>
          <header class="mb-4 flex items-center gap-3">
            <A href="/projects" class="text-sm text-primary hover:underline">
              ← 返回项目列表
            </A>
          </header>
          <WorkspaceProvider initial={e()}>
            <WizardShell projectId={id} step={step()} />
          </WorkspaceProvider>
        </main>
      )}
    </Show>
  );
}
