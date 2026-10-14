import { defineConfig, devices } from "@playwright/test";

// E2E 面向已启动的 SolidStart dev server（:7310）。若未启动则自动拉起 `pnpm dev`。
// 后端 FastAPI（:127.0.0.1:8917，需带 CORS_ALLOWED_ORIGINS=http://localhost:7310 启动）
// 不在此托管——E2E 断言真实读代理 + 客户端写/轮询链路，故要求后端先行。
const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? "http://localhost:7310";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  reporter: "list",
  use: {
    baseURL,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "pnpm dev",
    url: baseURL,
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
