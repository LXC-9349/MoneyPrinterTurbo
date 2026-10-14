import { Dialog } from "@kobalte/core/dialog";
import type { JSX } from "solid-js";
import { cn } from "~/lib/cn";

export interface DialogProps {
  open?: boolean;
  onOpenChange?: (v: boolean) => void;
  title?: JSX.Element;
  description?: JSX.Element;
  trigger?: JSX.Element;
  children?: JSX.Element;
  contentClass?: string;
}

// 模态对话框：设置弹窗等。受控 open + onOpenChange；Overlay 半透明遮罩，
// Content 居中卡片。Kobalte 负责焦点陷阱与 Esc 关闭（可访问性）。
export function DialogRoot(props: DialogProps) {
  return (
    <Dialog open={props.open} onOpenChange={props.onOpenChange}>
      {props.trigger != null && <Dialog.Trigger>{props.trigger}</Dialog.Trigger>}
      <Dialog.Portal>
        <Dialog.Overlay class="fixed inset-0 z-40 bg-black/55 data-[expanded]:animate-in data-[closed]:animate-out" />
        <div class="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-4">
          <Dialog.Content
            class={cn(
              "mt-[10vh] w-full max-w-2xl rounded-card border border-line bg-surface shadow-card",
              "data-[expanded]:animate-in data-[closed]:animate-out",
              props.contentClass,
            )}
          >
            <div class="flex items-center justify-between border-b border-line px-4 py-3">
              <Dialog.Title class="text-base font-bold text-ink">{props.title}</Dialog.Title>
              <Dialog.CloseButton class="rounded-control p-1 text-ink-dim hover:bg-surface-2 hover:text-ink">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
                  <path
                    d="M6 6l12 12M18 6L6 18"
                    stroke="currentColor"
                    stroke-width="2"
                    stroke-linecap="round"
                  />
                </svg>
              </Dialog.CloseButton>
            </div>
            {props.description != null && (
              <Dialog.Description class="px-4 pt-2 text-sm text-ink-dim">
                {props.description}
              </Dialog.Description>
            )}
            <div class="p-4">{props.children}</div>
          </Dialog.Content>
        </div>
      </Dialog.Portal>
    </Dialog>
  );
}
