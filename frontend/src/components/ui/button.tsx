import { Button as KButton } from "@kobalte/core/button";
import { splitProps, type ComponentProps, type JSX } from "solid-js";
import { cn } from "~/lib/cn";

// 按钮变体：primary（品牌蓝）/ secondary（表面 + 边框）/ ghost（无边框）/ danger（错误色）。
// 尺寸 sm/md。全部用静态 Tailwind 工具类映射到 @theme token，零运行时样式。
export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md";

const VARIANTS: Record<ButtonVariant, string> = {
  primary:
    "bg-primary text-white hover:bg-primary-hover shadow-[0_2px_10px_rgba(70,130,180,0.35)]",
  secondary: "bg-surface-2 text-ink border border-line hover:border-line-strong",
  ghost: "bg-transparent text-ink hover:bg-surface-2",
  danger: "bg-err text-white hover:brightness-110",
};

const SIZES: Record<ButtonSize, string> = {
  sm: "h-7 px-2.5 text-xs",
  md: "h-8 px-3.5 text-sm",
};

// 复用 Kobalte Button 的原生属性（onClick/disabled/type 等），叠加 variant/size 视觉维度。
export type ButtonProps = Omit<ComponentProps<typeof KButton>, "class" | "children"> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
  class?: string;
  children?: JSX.Element;
};

export function Button(p: ButtonProps) {
  const [local, rest] = splitProps(p, ["variant", "size", "class", "children"]);
  return (
    <KButton
      {...rest}
      class={cn(
        "inline-flex items-center justify-center gap-1.5 rounded-control font-semibold",
        "whitespace-nowrap transition-colors duration-120 select-none",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary",
        "data-[disabled]:pointer-events-none data-[disabled]:opacity-50",
        VARIANTS[local.variant ?? "secondary"],
        SIZES[local.size ?? "md"],
        local.class,
      )}
    >
      {local.children}
    </KButton>
  );
}
