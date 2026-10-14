import { For } from "solid-js";
import { cn } from "~/lib/cn";

export interface Step {
  id: string;
  label: string;
}

export interface StepperProps {
  steps: Step[];
  currentIndex: number;
  /** 已解锁可跳转的最大步序号（之前访问过的最远处）。 */
  maxUnlockedIndex: number;
  onSelect: (id: string, index: number) => void;
  class?: string;
}

// 分步向导步骤条：done(✓) / current(●) / todo(○) 三态。仅 currentIndex<=maxUnlocked
// 的芯片可点，避免跳未解锁步。纯受控，状态由上层 signal 驱动。
export function Stepper(props: StepperProps) {
  return (
    <div class={cn("grid gap-2", props.class)} style={{ "grid-template-columns": `repeat(${props.steps.length}, minmax(0, 1fr))` }}>
      <For each={props.steps}>
        {(s, i) => {
          const state = () =>
            i() === props.currentIndex ? "current" : i() < props.currentIndex ? "done" : "todo";
          const clickable = () => i() <= props.maxUnlockedIndex;
          return (
            <button
              type="button"
              disabled={!clickable()}
              onClick={() => clickable() && props.onSelect(s.id, i())}
              class={cn(
                "flex h-8 items-center justify-center gap-1.5 rounded-control border px-2 text-xs font-semibold",
                "transition-colors duration-120 outline-none focus-visible:ring-2 focus-visible:ring-primary/50",
                "whitespace-nowrap",
                state() === "current" && "border-primary bg-primary text-white",
                state() === "done" && "border-line-strong bg-surface-2 text-ink",
                state() === "todo" && "border-line bg-surface text-ink-dim",
                !clickable() && "cursor-not-allowed opacity-50",
              )}
            >
              <span aria-hidden>
                {state() === "done" ? "✓" : state() === "current" ? "●" : "○"}
              </span>
              <span>
                {i() + 1}. {s.label}
              </span>
            </button>
          );
        }}
      </For>
    </div>
  );
}
