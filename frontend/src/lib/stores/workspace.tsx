// 向导工作区状态：createStore 承载 draft（VideoParams 快照），Solid 的 store 代理
// 天然提供「字段级」细粒度更新——只有读取被改字段的组件会重绘，无需把每个字段拆成
// 独立 signal。派生态（校验门 / 摘要）用纯函数 + createMemo，避免 effect 级联。
// 写与轮询走客户端直连（~/lib/api/client），项目失效走 query 的 revalidate。
//
// 当前所处步骤由 URL（[id]/[step] 路由的 :step 参数）决定，不放进 store——这样
// 在四步间导航时 WorkspaceProvider 保持挂载、draft 编辑态不丢失。
import {
  createContext,
  createEffect,
  createMemo,
  createSignal,
  onCleanup,
  useContext,
  type Accessor,
  type JSX,
} from "solid-js";
import { createStore, produce } from "solid-js/store";
import * as api from "~/lib/api/client";
import { invalidateProjects } from "~/lib/api/data";
import {
  TaskState,
  WORKFLOW_STEPS,
  type ProjectEntry,
  type ProjectMode,
  type TaskStatus,
  type VideoParams,
  type WorkflowStep,
} from "~/lib/api/types";

export interface WorkspaceValue {
  id: string;
  entry: Accessor<ProjectEntry>;
  params: VideoParams;
  mode: Accessor<ProjectMode>;
  setField: <K extends keyof VideoParams>(key: K, value: VideoParams[K]) => void;
  patch: (partial: Partial<VideoParams>) => void;
  stepError: (step: WorkflowStep) => string;
  canProceed: (step: WorkflowStep) => boolean;
  isDirty: Accessor<boolean>;
  save: (opts?: { step?: WorkflowStep; status?: string; title?: string }) => Promise<void>;
  saving: Accessor<boolean>;
  task: Accessor<TaskStatus | null>;
  generating: Accessor<boolean>;
  submitGenerate: () => Promise<void>;
}

const WorkspaceCtx = createContext<WorkspaceValue>();

export function useWorkspace(): WorkspaceValue {
  const ctx = useContext(WorkspaceCtx);
  if (!ctx) throw new Error("useWorkspace 必须在 WorkspaceProvider 内使用");
  return ctx;
}

export function stepIndex(step: WorkflowStep): number {
  const i = WORKFLOW_STEPS.indexOf(step);
  return i < 0 ? 0 : i;
}

// 内容步硬校验：传记需选人物；普通需主题或文案之一。其余步骤恒通过。
// —— 沿用 webui/workspace._validate_step 的语义。导出为纯函数以便单测派生校验门。
export function contentError(params: VideoParams, mode: ProjectMode): string {
  if (mode === "biography") {
    return (params.biography_person || "").trim() ? "" : "请先在内容步选择传记人物";
  }
  const hasSubject = (params.video_subject || "").trim().length > 0;
  const hasScript = (params.video_script || "").trim().length > 0;
  return hasSubject || hasScript ? "" : "请填写视频主题，或先生成/粘贴文案";
}

export function WorkspaceProvider(props: {
  initial: ProjectEntry;
  children: JSX.Element;
}) {
  const [entry, setEntry] = createStore<ProjectEntry>(props.initial);
  const [params, setParams] = createStore<VideoParams>(props.initial.draft);
  const [task, setTask] = createStore<{ status: TaskStatus | null }>({ status: null });
  const [saving, setSaving] = createSignal(false);
  const [generating, setGenerating] = createSignal(false);
  const [dirty, setDirty] = createSignal(false);

  const mode: Accessor<ProjectMode> = createMemo(() => entry.mode);

  const setField: WorkspaceValue["setField"] = (key, value) => {
    setParams(key as never, value as never);
    setDirty(true);
  };
  const patch: WorkspaceValue["patch"] = (partial) => {
    setParams(produce((p) => Object.assign(p, partial)));
    setDirty(true);
  };

  const stepError = (s: WorkflowStep): string =>
    s === "content" ? contentError(params, entry.mode) : "";
  const canProceed = (s: WorkflowStep): boolean => stepError(s) === "";

  async function save(
    opts?: { step?: WorkflowStep; status?: string; title?: string },
  ): Promise<void> {
    setSaving(true);
    try {
      const updated = await api.patchProject(entry.id, {
        draft: { ...params },
        step: opts?.step,
        status: opts?.status,
        title: opts?.title,
        person: params.biography_person || undefined,
        language: params.video_language || undefined,
        source: params.video_source || undefined,
      });
      setEntry(updated);
      setDirty(false);
      invalidateProjects();
    } finally {
      setSaving(false);
    }
  }

  // 轮询：generating 为真且条目带 task_id 时，每 2s 定点拉取 GET /tasks/{id}，
  // 到终态即停并回写项目状态、revalidate 项目详情。cleanup 防止离开页面后继续跑。
  createEffect(() => {
    if (!generating()) return;
    const taskId = entry.task_id;
    if (!taskId) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      if (stopped) return;
      try {
        const st = await api.getTask(taskId);
        if (stopped) return;
        setTask("status", st);
        if (st.state === TaskState.COMPLETE || st.state === TaskState.FAILED) {
          setGenerating(false);
          const status = st.state === TaskState.COMPLETE ? "done" : "failed";
          try {
            const updated = await api.patchProject(entry.id, { status });
            setEntry(updated);
            invalidateProjects();
          } catch {
            /* 状态回写失败不阻断展示 */
          }
          return;
        }
      } catch {
        /* 单次轮询失败：下一拍重试 */
      }
      if (!stopped) timer = setTimeout(tick, 2000);
    };
    void tick();
    onCleanup(() => {
      stopped = true;
      if (timer) clearTimeout(timer);
    });
  });

  async function submitGenerate(): Promise<void> {
    const created = await api.createVideoTask({ ...params });
    const bound = await api.patchProject(entry.id, {
      draft: { ...params },
      step: "output",
      status: "processing",
      bind_task_id: created.task_id,
    });
    setEntry(bound);
    setTask("status", null);
    setGenerating(true);
    invalidateProjects();
  }

  const value: WorkspaceValue = {
    get id() {
      return entry.id;
    },
    entry: () => entry,
    params,
    mode,
    setField,
    patch,
    stepError,
    canProceed,
    isDirty: dirty,
    save,
    saving,
    task: createMemo(() => task.status),
    generating,
    submitGenerate,
  };

  return <WorkspaceCtx.Provider value={value}>{props.children}</WorkspaceCtx.Provider>;
}
