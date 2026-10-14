import { describe, expect, it, vi } from "vitest";
import { fireEvent, render } from "@solidjs/testing-library";
import { Button } from "~/components/ui/button";

describe("Button", () => {
  it("渲染文本并触发 onClick", () => {
    const onClick = vi.fn();
    const { getByRole } = render(() => <Button onClick={onClick}>保存</Button>);
    const btn = getByRole("button");
    expect(btn.textContent).toBe("保存");
    fireEvent.click(btn);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("disabled 时按钮禁用且点击不触发", () => {
    const onClick = vi.fn();
    const { getByRole } = render(
      () => <Button disabled onClick={onClick}>禁用</Button>,
    );
    const btn = getByRole("button") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    fireEvent.click(btn);
    expect(onClick).not.toHaveBeenCalled();
  });

  it("variant 映射到对应视觉工具类（零运行时样式）", () => {
    const { getByRole } = render(() => <Button variant="primary">p</Button>);
    expect(getByRole("button").className).toContain("bg-primary");
  });

  it("size=sm 应用小号尺寸类", () => {
    const { getByRole } = render(() => <Button size="sm">s</Button>);
    expect(getByRole("button").className).toContain("h-7");
  });
});
