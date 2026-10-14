// 轻量 className 合并：过滤 false/undefined 后拼接。够用即可，避免引入 clsx/twMerge
// 运行时依赖（性能优先）。冲突覆盖交由调用方保证顺序（base 在前、override 在后）。
export type ClassValue = string | false | null | undefined;

export function cn(...values: ClassValue[]): string {
  return values.filter(Boolean).join(" ");
}
