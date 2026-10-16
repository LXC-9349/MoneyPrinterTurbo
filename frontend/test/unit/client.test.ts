import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  confirmTask,
  createProject,
  getConfirmation,
  getTask,
  resolveApiUrl,
  selectConfirmationShot,
} from "~/lib/api/client";

const BASE = "http://127.0.0.1:8917";

afterEach(() => vi.unstubAllGlobals());

describe("resolveApiUrl", () => {
  it("绝对 URL 原样返回", () => {
    expect(resolveApiUrl("https://cdn.example.com/a.mp4")).toBe("https://cdn.example.com/a.mp4");
    expect(resolveApiUrl(`${BASE}/tasks/x.mp4`)).toBe(`${BASE}/tasks/x.mp4`);
  });

  it("相对路径拼到 PUBLIC_API_BASE，且归一化斜杠", () => {
    expect(resolveApiUrl("/tasks/a.mp4")).toBe(`${BASE}/tasks/a.mp4`);
    expect(resolveApiUrl("tasks/a.mp4")).toBe(`${BASE}/tasks/a.mp4`);
    expect(resolveApiUrl("///tasks/a.mp4")).toBe(`${BASE}/tasks/a.mp4`);
  });
});

describe("client 写/轮询：信封解包与错误", () => {
  it("createProject POST /projects 并解包 data", async () => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      expect(url).toBe(`${BASE}/api/v1/projects`);
      expect(init?.method).toBe("POST");
      expect(JSON.parse(String(init?.body))).toMatchObject({ mode: "normal" });
      return new Response(
        JSON.stringify({ status: 200, data: { id: "p_abc", mode: "normal" } }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const entry = await createProject("normal");
    expect(entry).toMatchObject({ id: "p_abc" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("信封 status>=400 抛 ApiError（HTTP 200 也判定）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ status: 422, message: "校验失败" }), { status: 200 }),
      ),
    );
    await expect(createProject("normal")).rejects.toBeInstanceOf(ApiError);
  });

  it("getTask GET /tasks/{id} 解包 data", async () => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      expect(url).toBe(`${BASE}/api/v1/tasks/t%201`);
      expect(init?.method).toBe("GET");
      return new Response(
        JSON.stringify({ status: 200, data: { task_id: "t 1", state: 4, progress: 10 } }),
        { status: 200 },
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const st = await getTask("t 1");
    expect(st).toMatchObject({ state: 4, progress: 10 });
  });

  it("非 JSON 响应按 HTTP 状态归类为 ApiError", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("boom", { status: 503 })));
    await expect(getTask("x")).rejects.toBeInstanceOf(ApiError);
  });
});

describe("分阶段确认（里程碑 4 #5）API 接线", () => {
  it("getConfirmation GET /tasks/{id}/confirmation 解包逐镜视图", async () => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      expect(url).toBe(`${BASE}/api/v1/tasks/t%201/confirmation`);
      expect(init?.method).toBe("GET");
      return new Response(
        JSON.stringify({
          status: 200,
          data: { task_id: "t 1", paused_stage: "materials", editable: true, shots: [], resumable_from: "video" },
        }),
        { status: 200 },
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const v = await getConfirmation("t 1");
    expect(v).toMatchObject({ paused_stage: "materials", resumable_from: "video" });
  });

  it("confirmTask POST /tasks/{id}/confirm 返回续跑起点", async () => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      expect(url).toBe(`${BASE}/api/v1/tasks/t9/confirm`);
      expect(init?.method).toBe("POST");
      return new Response(
        JSON.stringify({ status: 200, data: { task_id: "t9", resumed_from: "video", state: 1 } }),
        { status: 200 },
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const r = await confirmTask("t9");
    expect(r).toMatchObject({ resumed_from: "video", state: 1 });
  });

  it("selectConfirmationShot POST 逐镜 select 带候选 url", async () => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      expect(url).toBe(`${BASE}/api/v1/tasks/t9/confirmation/shots/2/select`);
      expect(init?.method).toBe("POST");
      expect(JSON.parse(String(init?.body))).toMatchObject({ url: "https://x/y.png" });
      return new Response(
        JSON.stringify({ status: 200, data: { index: 2, materials: ["a", "b", "/t/y.png"] } }),
        { status: 200 },
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const r = await selectConfirmationShot("t9", 2, { url: "https://x/y.png" });
    expect(r.materials[2]).toBe("/t/y.png");
  });

  it("409（非待确认态）抛 ApiError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ status: 409, message: "not awaiting" }), { status: 409 })),
    );
    await expect(confirmTask("t9")).rejects.toBeInstanceOf(ApiError);
  });
});
