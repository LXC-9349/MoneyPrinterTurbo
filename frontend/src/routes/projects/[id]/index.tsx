// /projects/{id} → 重定向到条目当前步 /projects/{id}/{step}。entry.step 来自
// query 的项目详情（SSR 阶段即可解析），解析后客户端 replace 导航，避免留下多余的
// 历史记录条目。
import { createEffect } from "solid-js";
import { createAsync, useNavigate, useParams } from "@solidjs/router";
import { projectQuery } from "~/lib/api/data";
import { WORKFLOW_STEPS, type WorkflowStep } from "~/lib/api/types";

export default function ProjectRedirect() {
  const routeParams = useParams();
  const navigate = useNavigate();
  const id = routeParams.id!;
  const entry = createAsync(() => projectQuery(id));

  // 解析完成即跳转到当前步；非法 step 回退到 content。
  createEffect(() => {
    const e = entry();
    if (!e) return;
    const step: WorkflowStep = WORKFLOW_STEPS.includes(e.step) ? e.step : "content";
    navigate(`/projects/${id}/${step}`, { replace: true });
  });

  return (
    <main class="mx-auto max-w-5xl px-6 py-10">
      <p class="text-sm text-ink-dim">正在打开项目…</p>
    </main>
  );
}
