import type { JSX } from "solid-js";
import { cn } from "~/lib/cn";

// 多行文本：脚本/文案输入用。受控 value + onInput；label 通过 htmlFor 关联。
export interface TextAreaProps {
  id: string;
  label?: JSX.Element;
  value?: string;
  onInput?: (v: string) => void;
  placeholder?: string;
  rows?: number;
  disabled?: boolean;
  class?: string;
}

export function TextArea(props: TextAreaProps) {
  return (
    <div class={cn("flex flex-col gap-1", props.class)}>
      {props.label != null && (
        <label for={props.id} class="text-[0.82rem] font-medium text-ink-dim">
          {props.label}
        </label>
      )}
      <textarea
        id={props.id}
        value={props.value}
        onInput={(e) => props.onInput?.(e.currentTarget.value)}
        placeholder={props.placeholder}
        rows={props.rows ?? 5}
        disabled={props.disabled}
        class={cn(
          "w-full resize-y rounded-control border border-line bg-surface-2 px-2.5 py-2 text-sm",
          "leading-relaxed text-ink placeholder:text-ink-dim/70 outline-none",
          "transition-colors duration-120 focus:border-line-strong focus:ring-2 focus:ring-primary/40",
          "disabled:opacity-50",
        )}
      />
    </div>
  );
}
