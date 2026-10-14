import { toaster, Toast } from "@kobalte/core/toast";
import { Show } from "solid-js";
import { cn } from "~/lib/cn";

// 全局轻提示：v0.13 起 Kobalte 用 `toaster` 单例 + `Toast.Region/List` 渲染。
// notify.success/error 便捷封装同一套样式与自动关闭时长。
type Tone = "success" | "danger" | "info";

function ToastCard(props: {
  toastId: number;
  tone: Tone;
  title: string;
  description?: string;
  duration: number;
}) {
  return (
    <Toast
      toastId={props.toastId}
      duration={props.duration}
      class={cn(
        "pointer-events-auto flex flex-col gap-1 rounded-card border bg-surface p-3 shadow-card",
        props.tone === "success" && "border-ok/50",
        props.tone === "danger" && "border-err/50",
        props.tone === "info" && "border-line",
      )}
    >
      <div class="flex items-start justify-between gap-2">
        <Toast.Title class="text-sm font-semibold text-ink">{props.title}</Toast.Title>
        <Toast.CloseButton class="text-ink-dim hover:text-ink" aria-label="关闭">
          ✕
        </Toast.CloseButton>
      </div>
      <Show when={props.description}>
        <Toast.Description class="text-xs text-ink-dim">{props.description}</Toast.Description>
      </Show>
    </Toast>
  );
}

export const notify = {
  success(title: string, description?: string) {
    return toaster.show((p) => (
      <ToastCard toastId={p.toastId} tone="success" title={title} description={description} duration={3500} />
    ));
  },
  error(title: string, description?: string) {
    return toaster.show((p) => (
      <ToastCard toastId={p.toastId} tone="danger" title={title} description={description} duration={5000} />
    ));
  },
  info(title: string, description?: string) {
    return toaster.show((p) => (
      <ToastCard toastId={p.toastId} tone="info" title={title} description={description} duration={4000} />
    ));
  },
};

export function Toaster(props: { region?: string }) {
  return (
    <Toast.Region
      regionId={props.region}
      aria-label="通知"
      class="pointer-events-none fixed bottom-4 right-4 z-[60] w-80"
    >
      <Toast.List class="pointer-events-auto flex flex-col gap-2" />
    </Toast.Region>
  );
}
