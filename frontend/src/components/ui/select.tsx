import { Select as KSelect } from "@kobalte/core/select";
import type { JSX } from "solid-js";
import { cn } from "~/lib/cn";

export interface SelectOption {
  value: string;
  label: string;
}

export interface SelectProps {
  label?: JSX.Element;
  value?: string | null;
  options: SelectOption[];
  onChange?: (v: string | null) => void;
  placeholder?: string;
  disabled?: boolean;
  class?: string;
}

// 下拉选择：基于 Kobalte Select，端口化（Portal 到 body，避免被卡片 overflow 裁切）。
// 选项以 {value,label} 传入；Kobalte 以「原始 option 对象」建模，这里做双向映射。
export function Select(props: SelectProps) {
  const selected = () => props.options.find((o) => o.value === props.value) ?? null;
  return (
    <KSelect
      options={props.options}
      optionValue="value"
      optionTextValue="label"
      value={selected()}
      onChange={(opt) => props.onChange?.(opt?.value ?? null)}
      placeholder={props.placeholder}
      disabled={props.disabled}
      class={cn("flex flex-col gap-1", props.class)}
      itemComponent={(p) => (
        <KSelect.Item
          item={p.item}
          class={cn(
            "relative flex cursor-pointer items-center rounded-control px-2 py-1.5 text-sm",
            "text-ink outline-none data-[highlighted]:bg-surface-2",
            "data-[disabled]:opacity-50",
          )}
        >
          <KSelect.ItemLabel>{p.item.rawValue.label}</KSelect.ItemLabel>
          <KSelect.ItemIndicator class="ml-auto text-primary" aria-label="已选">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden>
              <path
                d="M5l5 5 9-9"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              />
            </svg>
          </KSelect.ItemIndicator>
        </KSelect.Item>
      )}
    >
      {props.label != null && (
        <KSelect.Label class="text-[0.82rem] font-medium text-ink-dim">
          {props.label}
        </KSelect.Label>
      )}
      <KSelect.Trigger
        class={cn(
          "flex h-8 w-full items-center justify-between gap-2 rounded-control border border-line",
          "bg-surface-2 px-2.5 text-sm text-ink outline-none transition-colors duration-120",
          "hover:border-line-strong data-[expanded]:border-line-strong data-[expanded]:ring-2 data-[expanded]:ring-primary/40",
          "data-[disabled]:opacity-50",
        )}
      >
        <KSelect.Value class="data-[placeholder-shown]:text-ink-dim/70" />
        <KSelect.Icon class="text-ink-dim">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" aria-hidden>
            <path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
          </svg>
        </KSelect.Icon>
      </KSelect.Trigger>
      <KSelect.Portal>
        <KSelect.Content
          class={cn(
            "z-50 mt-1 max-h-64 min-w-[var(--kb-select-trigger-width)] overflow-hidden",
            "rounded-card border border-line bg-surface shadow-card",
          )}
        >
          <KSelect.Listbox class="max-h-64 overflow-y-auto p-1" />
        </KSelect.Content>
      </KSelect.Portal>
    </KSelect>
  );
}
