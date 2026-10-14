import { describe, expect, it, vi } from "vitest";
import { fireEvent, render } from "@solidjs/testing-library";
import { Stepper, type Step } from "~/components/ui/stepper";

const STEPS: Step[] = [
  { id: "content", label: "内容" },
  { id: "visual", label: "视觉" },
  { id: "audio", label: "声音" },
];

describe("Stepper", () => {
  it("渲染每一步的编号与标签", () => {
    const { getByText } = render(
      () => <Stepper steps={STEPS} currentIndex={1} maxUnlockedIndex={1} onSelect={vi.fn()} />,
    );
    expect(getByText("1. 内容")).toBeTruthy();
    expect(getByText("2. 视觉")).toBeTruthy();
    expect(getByText("3. 声音")).toBeTruthy();
  });

  it("已解锁步点击回调 (id, index)", () => {
    const onSelect = vi.fn();
    const { getByText } = render(
      () => <Stepper steps={STEPS} currentIndex={0} maxUnlockedIndex={1} onSelect={onSelect} />,
    );
    fireEvent.click(getByText("2. 视觉"));
    expect(onSelect).toHaveBeenCalledWith("visual", 1);
  });

  it("未解锁步禁用且不触发 onSelect", () => {
    const onSelect = vi.fn();
    const { getByText } = render(
      () => <Stepper steps={STEPS} currentIndex={0} maxUnlockedIndex={0} onSelect={onSelect} />,
    );
    fireEvent.click(getByText("3. 声音"));
    expect(onSelect).not.toHaveBeenCalled();
  });
});
