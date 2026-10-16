// 旧向导路由（里程碑 2 起停用）：/projects/{id} 解析条目后重定向到新界面——
// 传记项目回人物详情页 /persons/:person，已绑定任务回 /tasks/:task_id，其余回首页 /。
import { createEffect } from "solid-js";
import { createAsync, useNavigate, useParams } from "@solidjs/router";
import { projectQuery } from "~/lib/api/data";

export default function ProjectRedirect() {
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
