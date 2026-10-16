// 旧向导项目列表（里程碑 2 起停用）：/projects 直接重定向到新的工作台首页 /。
// 人物聚合已覆盖原列表（projects.json → tasks 表一次性迁移），生成入口改由人物详情页提供。
import { createEffect } from "solid-js";
import { useNavigate } from "@solidjs/router";

export default function ProjectsIndexRedirect() {
  const navigate = useNavigate();
  createEffect(() => navigate("/", { replace: true }));
  return (
    <main class="mx-auto max-w-5xl px-6 py-10">
      <p class="text-sm text-ink-dim">正在打开工作台…</p>
    </main>
  );
}
