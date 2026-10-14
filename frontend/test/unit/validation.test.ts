import { describe, expect, it } from "vitest";
import { contentError, stepIndex } from "~/lib/stores/workspace";
import { WORKFLOW_STEPS, type ProjectMode, type VideoParams } from "~/lib/api/types";

// 只填充 contentError 实际读取的字段，其余用 as 断言为完整 VideoParams——
// 这是纯函数单测，无需构造后端全量 draft。
function draft(partial: Partial<VideoParams>): VideoParams {
  return partial as VideoParams;
}

describe("stepIndex", () => {
  it("按 WORKFLOW_STEPS 顺序返回下标", () => {
    expect(stepIndex("content")).toBe(0);
    expect(stepIndex("visual")).toBe(1);
    expect(stepIndex("audio")).toBe(2);
    expect(stepIndex("output")).toBe(WORKFLOW_STEPS.length - 1);
  });
});

describe("contentError（沿用 _validate_step 语义）", () => {
  it("普通模式：主题与文案都空 → 报错", () => {
    const msg = contentError(draft({ video_subject: "", video_script: "" }), "normal");
    expect(msg).not.toBe("");
  });

  it("普通模式：仅有主题 → 通过", () => {
    expect(contentError(draft({ video_subject: "春天的花海" }), "normal")).toBe("");
  });

  it("普通模式：主题为空但有文案 → 通过", () => {
    expect(contentError(draft({ video_subject: "", video_script: "已生成文案" }), "normal")).toBe(
      "",
    );
  });

  it("普通模式：仅空白字符视为空", () => {
    expect(contentError(draft({ video_subject: "   ", video_script: "  " }), "normal")).not.toBe(
      "",
    );
  });

  it("传记模式：未选人物 → 报错（与主题/文案无关）", () => {
    const mode: ProjectMode = "biography";
    expect(contentError(draft({ biography_person: "", video_subject: "x", video_script: "y" }), mode)).not.toBe(
      "",
    );
  });

  it("传记模式：已选人物 → 通过", () => {
    expect(contentError(draft({ biography_person: "李白" }), "biography")).toBe("");
  });
});
