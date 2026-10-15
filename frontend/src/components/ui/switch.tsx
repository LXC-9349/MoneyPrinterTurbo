import { Switch } from "@kobalte/core/switch";
import type { JSX } from "solid-js";
import { cn } from "~/lib/cn";

export interface SwitchProps {
  label?: JSX.Element;
  checked?: boolean;
  onChange?: (v: boolean) => void;
  disabled?: boolean;
  class?: string;
}

// 开关：布尔型参数（如 match_materials_to_script）。受控 checked + onChange。
// 注意：label 请传纯字符串。在 <For> 循环内传 JSX 元素作 label 会在 dev
// 水合时触发 solid “template is not a function”整页白屏（里程碑 2 验收实测，
// 生产构建正常故仅坑 dev）；字号差异用 class 调，不要包 <span>。
export function KSwitch(props: SwitchProps) {
  return (
    <Switch
      checked={props.checked}
      onChange={props.onChange}
      disabled={props.disabled}
      class={cn("flex items-center gap-2", props.class)}
    >
      <Switch.Control
        class={cn(
          "flex h-5 w-9 shrink-0 items-center rounded-full border border-line px-0.5",
          "bg-surface-2 outline-none transition-colors duration-120",
          "data-[checked]:border-primary data-[checked]:bg-primary",
          "data-[disabled]:opacity-50",
        )}
      >
        <Switch.Thumb
          class={cn(
            "block h-3.5 w-3.5 rounded-full bg-ink transition-transform duration-120",
            "data-[checked]:translate-x-4 data-[checked]:bg-white",
          )}
        />
      </Switch.Control>
      {props.label != null && (
        <Switch.Label class="text-sm text-ink">{props.label}</Switch.Label>
      )}
    </Switch>
  );
}
