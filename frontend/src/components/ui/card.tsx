import type { JSX } from "solid-js";
import { cn } from "~/lib/cn";

// 卡片：工作室仪表盘容器（表面色 + 细边框 + 圆角 + 阴影）。纯静态样式。
export function Card(props: { class?: string; children?: JSX.Element }) {
  return (
    <div
      class={cn(
        "rounded-card border border-line bg-surface shadow-card",
        "transition-colors duration-120 hover:border-line-strong",
        props.class,
      )}
    >
      {props.children}
    </div>
  );
}

export function CardHeader(props: { class?: string; children?: JSX.Element }) {
  return <div class={cn("px-4 pt-3.5 pb-1", props.class)}>{props.children}</div>;
}

export function CardTitle(props: { class?: string; children?: JSX.Element }) {
  return (
    <h3 class={cn("text-base font-bold leading-tight text-ink", props.class)}>
      {props.children}
    </h3>
  );
}

export function CardContent(props: { class?: string; children?: JSX.Element }) {
  return <div class={cn("px-4 pb-4", props.class)}>{props.children}</div>;
}
