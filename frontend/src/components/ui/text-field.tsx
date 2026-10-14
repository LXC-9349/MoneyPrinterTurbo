import { TextField as KTextField } from "@kobalte/core/text-field";
import type { JSX } from "solid-js";
import { cn } from "~/lib/cn";

// 文本输入：基于 Kobalte TextField，自动关联 label/description/error 的 aria。
// 受控用法：传 value + onInput。字段级 signal 直接绑定，保证细粒度更新只重绘该输入。
export interface TextFieldProps {
  label?: JSX.Element;
  description?: JSX.Element;
  error?: JSX.Element;
  value?: string;
  onInput?: (v: string) => void;
  type?: "text" | "email" | "number" | "password" | "search" | "url";
  placeholder?: string;
  disabled?: boolean;
  required?: boolean;
  class?: string;
}

export function TextField(props: TextFieldProps) {
  return (
    <KTextField
      value={props.value}
      onChange={props.onInput}
      disabled={props.disabled}
      required={props.required}
      class={cn("flex flex-col gap-1", props.class)}
    >
      {props.label != null && (
        <KTextField.Label class="text-[0.82rem] font-medium text-ink-dim">
          {props.label}
        </KTextField.Label>
      )}
      <KTextField.Input
        type={props.type ?? "text"}
        placeholder={props.placeholder}
        class={cn(
          "h-8 w-full rounded-control border border-line bg-surface-2 px-2.5 text-sm text-ink",
          "placeholder:text-ink-dim/70 outline-none transition-colors duration-120",
          "focus:border-line-strong focus:ring-2 focus:ring-primary/40",
          "data-[disabled]:opacity-50",
        )}
      />
      {props.description != null && (
        <KTextField.Description class="text-xs text-ink-dim">
          {props.description}
        </KTextField.Description>
      )}
      {props.error != null && (
        <KTextField.ErrorMessage class="text-xs text-err">
          {props.error}
        </KTextField.ErrorMessage>
      )}
    </KTextField>
  );
}
