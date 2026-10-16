// 旧向导分步路由（里程碑 2 起停用）：/projects/{id}/{step} 一律重定向到新界面。
// 人物编辑与生成改由人物详情页 + 创建任务弹窗承载；解析条目后按人物/任务回跳。
import { createEffect } from "solid-js";
import { createAsync, useNavigate, useParams } from "@solidjs/router";
import { projectQuery } from "~/lib/api/data";

export default function ProjectStepRedirect() {
  const routeParams = useParams();
  const navigate = useNavigate();
  const id = routeParams.id!;
  const entry = createAsync(() => projectQuery(id));

  createEffect(() => {
    const e = entry();
    if (!e) return;
    if (e.person) navigate(`/persons/${encodeURIComponent(e.person)}`, { replace: true });
    else if (e.task_id) navigate(`/tasks/${encodeURIComponent(e.task_id)}`, { replace: true });
    else navigate("/", { replace: true });
  });

  return (
    <main class="mx-auto max-w-5xl px-6 py-10">
      <p class="text-sm text-ink-dim">正在打开人物详情…</p>
    </main>
  );
}
