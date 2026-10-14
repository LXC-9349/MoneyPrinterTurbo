import { describe, expect, it, vi } from "vitest";
import { fireEvent, render } from "@solidjs/testing-library";
import { TextField } from "~/components/ui/text-field";
import { KSwitch } from "~/components/ui/switch";

describe("TextField（受控字段级绑定）", () => {
  it("渲染 label 与 value，并通过 aria 关联", () => {
    const { getByLabelText } = render(
      () => <TextField label="视频主题" value="春天的花海" onInput={vi.fn()} />,
    );
    const input = getByLabelText("视频主题") as HTMLInputElement;
    expect(input.value).toBe("春天的花海");
  });

  it("输入触发 onInput(新值)", () => {
    const onInput = vi.fn();
    const { getByLabelText } = render(
      () => <TextField label="视频主题" value="" onInput={onInput} />,
    );
    fireEvent.input(getByLabelText("视频主题"), { target: { value: "abc" } });
    expect(onInput).toHaveBeenCalledWith("abc");
  });
});

describe("KSwitch（布尔参数）", () => {
  it("渲染标签与开关控件", () => {
    const { getByText, container } = render(
      () => <KSwitch label="按顺序匹配" checked={false} onChange={vi.fn()} />,
    );
    expect(getByText("按顺序匹配")).toBeTruthy();
    expect(container.querySelector('[id$="-control"]')).toBeTruthy();
  });

  it("点击控件触发 onChange(下一状态)", () => {
    const onChange = vi.fn();
    const { container } = render(
      () => <KSwitch label="匹配" checked={false} onChange={onChange} />,
    );
    const control = container.querySelector('[id$="-control"]') as HTMLElement;
    fireEvent.click(control);
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0][0]).toBe(true);
  });

  it("禁用时控件带 data-disabled 且不触发 onChange", () => {
    const onChange = vi.fn();
    const { container } = render(
      () => <KSwitch label="x" checked={false} disabled onChange={onChange} />,
    );
    const control = container.querySelector('[id$="-control"]') as HTMLElement;
    expect(control.hasAttribute("data-disabled")).toBe(true);
    fireEvent.click(control);
    expect(onChange).not.toHaveBeenCalled();
  });
});
