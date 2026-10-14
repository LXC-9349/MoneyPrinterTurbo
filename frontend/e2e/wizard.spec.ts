import { expect, test, type APIRequestContext } from "@playwright/test";

// 向导端到端：覆盖「建项目 → 四步导航 → 提交生成 → 轮询开始」主流程，以及内容步
// 校验门、传记模式人物选择器两条关键路径。后端(:8917)须先行且带
// CORS_ALLOWED_ORIGINS=http://localhost:7310（供浏览器写/轮询直连）。
// 用例结束后经服务端 DELETE 清理所建项目（Node 侧请求，无 CORS 约束）。
const API = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8917";

function idFromUrl(url: string): string {
  const m = url.match(/p_[0-9a-f]+/);
  if (!m) throw new Error("未在项目 URL 中解析到 id: " + url);
  return m[0];
}

async function deleteProject(request: APIRequestContext, id: string): Promise<void> {
  await request.delete(`${API}/api/v1/projects/${id}`);
}

test.describe("项目工作区向导", () => {
  test("列表渲染并含新建入口", async ({ page }) => {
    await page.goto("/projects");
    await expect(page.getByRole("heading", { name: "项目工作区" })).toBeVisible();
    await expect(page.getByRole("button", { name: "＋ 普通项目" })).toBeVisible();
    await expect(page.getByRole("button", { name: "＋ 传记项目" })).toBeVisible();
  });

  test("普通模式：校验门 + 四步导航 + 提交生成轮询", async ({ page, request }) => {
    await page.goto("/projects");
    await page.getByRole("button", { name: "＋ 普通项目" }).click();
    // SPA 路由用 history.pushState，不触发 load 事件，故用轮询式 toHaveURL。
    await expect(page).toHaveURL(/\/projects\/p_[0-9a-f]+\/content$/);
    const id = idFromUrl(page.url());

    try {
      const next = page.getByRole("button", { name: "下一步" });

      // 校验门：主题/文案皆空时「下一步」禁用（沿用 _validate_step）。
      await expect(next).toBeDisabled();

      // 填写主题 → 校验通过，前进到视觉。
      await page.getByLabel("视频主题").fill("春天的花海");
      await expect(next).toBeEnabled();
      await next.click();
      await expect(page).toHaveURL(/\/visual$/);

      // 视觉 → 声音 → 字幕与生成（这三步无硬校验门）。
      await page.getByRole("button", { name: "下一步" }).click();
      await expect(page).toHaveURL(/\/audio$/);
      await page.getByRole("button", { name: "下一步" }).click();
      await expect(page).toHaveURL(/\/output$/);

      // 提交生成：客户端 POST /videos → 进入轮询，出现「进行中」徽标。
      await page.getByRole("button", { name: "开始生成" }).click();
      await expect(page.getByText("进行中")).toBeVisible({ timeout: 30_000 });
    } finally {
      await deleteProject(request, id);
    }
  });

  test("传记模式：内容步提供人物选择器", async ({ page, request }) => {
    await page.goto("/projects");
    await page.getByRole("button", { name: "＋ 传记项目" }).click();
    await expect(page).toHaveURL(/\/projects\/p_[0-9a-f]+\/content$/);
    const id = idFromUrl(page.url());
    try {
      await expect(page.getByLabel("传记人物")).toBeVisible();
      await expect(page.getByText("搜索人物")).toBeVisible();
    } finally {
      await deleteProject(request, id);
    }
  });
});
