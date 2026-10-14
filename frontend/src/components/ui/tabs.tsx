import { Tabs } from "@kobalte/core/tabs";
import type { JSX } from "solid-js";
import { cn } from "~/lib/cn";

export interface TabItem {
  value: string;
  label: JSX.Element;
}

export interface TabsProps {
  value?: string;
  onChange?: (v: string) => void;
  items: TabItem[];
  class?: string;
  children?: JSX.Element;
}

// 标签页：素材库文档/人物、设置分类等。List 做下划线选中态；Panel 由调用方放 children。
export function TabsRoot(props: TabsProps) {
  return (
    <Tabs value={props.value} onChange={props.onChange} class={cn("flex flex-col", props.class)}>
      <Tabs.List class="flex gap-1 border-b border-line">
        {props.items.map((it) => (
          <Tabs.Trigger
            value={it.value}
            class={cn(
              "relative -mb-px rounded-t-control px-3 py-1.5 text-sm font-semibold text-ink-dim",
              "outline-none transition-colors duration-120 hover:text-ink",
              "data-[selected]:text-ink data-[selected]:border-b-2 data-[selected]:border-primary",
            )}
          >
            {it.label}
          </Tabs.Trigger>
        ))}
      </Tabs.List>
      {props.children}
    </Tabs>
  );
}

export const TabPanel = Tabs.Content;
