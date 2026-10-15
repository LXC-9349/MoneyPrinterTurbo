import { Slider } from "@kobalte/core/slider";
import type { JSX } from "solid-js";
import { cn } from "~/lib/cn";

export interface SliderProps {
  label?: JSX.Element;
  value: number;
  onChange?: (v: number) => void;
  minValue?: number;
  maxValue?: number;
  step?: number;
  formatValue?: (v: number) => string;
  disabled?: boolean;
  class?: string;
}

// 单值滑块：语速/音量等。Kobalte Slider 以数组建模，这里封装成单值受控。
// v0.13 结构：Slider > Track > Fill，Thumb 与 Track 平级；ValueLabel 仅接受静态 children。
// label 同样只传纯字符串，原因见 switch.tsx 注释（dev 水合白屏坑）。
export function KSlider(props: SliderProps) {
  const fmt = () => (props.formatValue ?? ((n: number) => String(n)))(props.value);
  return (
    <Slider
      value={[props.value]}
      onChange={(arr) => props.onChange?.(arr[0])}
      minValue={props.minValue ?? 0}
      maxValue={props.maxValue ?? 100}
      step={props.step ?? 1}
      disabled={props.disabled}
      class={cn("flex flex-col gap-1", props.class)}
    >
      {props.label != null && (
        <div class="flex items-center justify-between">
          <Slider.Label class="text-[0.82rem] font-medium text-ink-dim">
            {props.label}
          </Slider.Label>
          <span class="text-xs tabular-nums text-ink">{fmt()}</span>
        </div>
      )}
      <div class="relative flex h-6 w-full touch-none items-center select-none">
        <Slider.Track class="relative h-1.5 w-full grow overflow-hidden rounded-full bg-surface-2">
          <Slider.Fill class="absolute h-full bg-primary" />
        </Slider.Track>
        <Slider.Thumb
          class={cn(
            "block h-4 w-4 rounded-full border-2 border-primary bg-white outline-none",
            "shadow focus-visible:ring-2 focus-visible:ring-primary/50",
            "data-[disabled]:opacity-50",
          )}
        />
      </div>
    </Slider>
  );
}
