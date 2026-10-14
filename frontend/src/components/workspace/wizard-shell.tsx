// 向导外壳：步骤条 + 当前步面板 + 前进/后退导航 + 右侧摘要轨。导航在切换步骤前
// 触发「前进校验 + 落盘 draft」（沿用 webui 的 _commit_draft + _validate_step），
// 校验不通过则拦截并提示。四个步骤面板用 lazy() 按当前步动态加载（代码分割），
// 未到达的步骤不进初始包。
import { Show, Suspense, createSignal, lazy } from "solid-js";
import { useNavigate } from "@solidjs/router";
import { Stepper, type Step } from "~/components/ui/stepper";
import { Button } from "~/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "~/components/ui/card";
import { notify } from "~/components/ui/toast";
import { WORKFLOW_STEPS, STEP_LABELS, type WorkflowStep } from "~/lib/api/types";
import { stepIndex, useWorkspace } from "~/lib/stores/workspace";

// 步骤面板按需动态导入：dynamic import 需默认导出，这里把命名导出适配为 default。
const ContentStep = lazy(() => import("./content-step").then((m) => ({ default: m.ContentStep })));
const VisualStep = lazy(() => import("./visual-step").then((m) => ({ default: m.VisualStep })));
const AudioStep = lazy(() => import("./audio-step").then((m) => ({ default: m.AudioStep })));
const OutputStep = lazy(() => import("./output-step").then((m) => ({ default: m.OutputStep })));

const STEPS: Step[] = WORKFLOW_STEPS.map((s) => ({ id: s, label: STEP_LABELS[s] }));

export function WizardShell(props: { projectId: string; step: WorkflowStep }) {
  const ws = useWorkspace();
  const navigate = useNavigate();
  const [maxUnlocked, setMaxUnlocked] = createSignal(stepIndex(props.step));

  // 切到目标步：先校验当前步（前进才校验），落盘 draft，再更新 URL。
  async function goTo(target: WorkflowStep, validateFrom: WorkflowStep) {
    if (WORKFLOW_STEPS.indexOf(target) > WORKFLOW_STEPS.indexOf(validateFrom)) {
      const err = ws.stepError(validateFrom);
      if (err) {
        notify.error("无法前进", err);
        return;
      }
    }
    try {
      await ws.save({ step: target });
      setMaxUnlocked((m) => Math.max(m, stepIndex(target)));
      navigate(`/projects/${props.projectId}/${target}`);
    } catch {
      notify.error("保存失败", "请检查后端连接后重试");
    }
  }

  const idx = () => stepIndex(props.step);
  const prevStep = () => (idx() > 0 ? WORKFLOW_STEPS[idx() - 1] : undefined);
  const nextStep = () =>
    idx() < WORKFLOW_STEPS.length - 1 ? WORKFLOW_STEPS[idx() + 1] : undefined;

  return (
    <div class="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_260px]">
      <Card class="lg:col-span-1">
        <CardHeader>
          <Stepper
            steps={STEPS}
            currentIndex={idx()}
            maxUnlockedIndex={maxUnlocked()}
            onSelect={(id) => void goTo(id as WorkflowStep, props.step)}
          />
        </CardHeader>
        <CardContent>
          <Suspense fallback={<PanelSkeleton />}>
            <Show when={props.step === "content"}>
              <ContentStep projectId={props.projectId} />
            </Show>
            <Show when={props.step === "visual"}>
              <VisualStep />
            </Show>
            <Show when={props.step === "audio"}>
              <AudioStep />
            </Show>
            <Show when={props.step === "output"}>
              <OutputStep />
            </Show>
          </Suspense>

          <div class="mt-6 flex items-center justify-between">
            <Button
              variant="ghost"
              disabled={!prevStep()}
              onClick={() => prevStep() && void goTo(prevStep()!, props.step)}
            >
              上一步
            </Button>
            <Show
              when={nextStep()}
              fallback={
                <span class="text-xs text-ink-dim">已在最后一步，可开始生成</span>
              }
            >
              <Button
                variant="primary"
                disabled={!ws.canProceed(props.step)}
                onClick={() => nextStep() && void goTo(nextStep()!, props.step)}
              >
                下一步
              </Button>
            </Show>
          </div>
        </CardContent>
      </Card>

      <aside class="lg:sticky lg:top-4 lg:self-start">
        <Card>
          <CardHeader>
            <CardTitle class="text-sm">摘要</CardTitle>
          </CardHeader>
          <CardContent class="flex flex-col gap-2 text-sm">
            <Row
              icon={ws.mode() === "biography" ? "👤" : "🎯"}
              value={ws.mode() === "biography" ? ws.params.biography_person : ws.params.video_subject}
            />
            <Row icon="🌐" value={ws.params.video_language} />
            <Row icon="🎞️" value={ws.params.video_source} />
            <Row icon="🎙️" value={ws.params.voice_name} />
            <Show when={ws.isDirty()}>
              <p class="text-xs text-warn">有未保存改动（导航时自动保存）</p>
            </Show>
          </CardContent>
        </Card>
      </aside>
    </div>
  );
}

function Row(props: { icon: string; value: string }) {
  return (
    <p class="flex items-center gap-2 text-ink-dim">
      <span aria-hidden>{props.icon}</span>
      <span class="truncate text-ink">{props.value || "—"}</span>
    </p>
  );
}

// 面板动态模块加载时的骨架（预留高度，避免 CLS）。
function PanelSkeleton() {
  return (
    <div class="flex min-h-64 flex-col gap-3 py-2">
      <div class="h-8 w-1/3 animate-pulse rounded-control bg-surface-2" />
      <div class="h-16 w-full animate-pulse rounded-control bg-surface-2" />
      <div class="h-16 w-full animate-pulse rounded-control bg-surface-2" />
    </div>
  );
}
