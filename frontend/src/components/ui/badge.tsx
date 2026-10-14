import type { JSX } from "solid-js";
import { cn } from "~/lib/cn";

// 状态徽章：草稿/进行中/已完成/失败等。tone 映射到语义色。
export type BadgeTone = "neutral" | "primary" | "ok" | "warn" | "err";

const TONES: Record<BadgeTone, string> = {
  neutral: "bg-surface-2 text-ink-dim border-line",
  primary: "bg-primary/15 text-ink border-line-strong",
  ok: "bg-ok/15 text-ok border-ok/40",
  warn: "bg-warn/15 text-warn border-warn/40",
  err: "bg-err/15 text-err border-err/40",
};

export function Badge(props: { tone?: BadgeTone; class?: string; children?: JSX.Element }) {
  return (
    <span
      class={cn(
        "inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold whitespace-nowrap",
        TONES[props.tone ?? "neutral"],
        props.class,
      )}
    >
      {props.children}
    </span>
  );
}
