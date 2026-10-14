import { defineConfig } from "vitest/config";
import solid from "vite-plugin-solid";
import tsconfigPaths from "vite-tsconfig-paths";

// 单元/组件测试专用配置：用 vite-plugin-solid 编译 Solid JSX，jsdom 提供 DOM，
// 刻意不复用 vite.config.ts 的 solidStart()（SSR/文件路由插件会干扰测试转换）。
// ~/* 别名经 vite-tsconfig-paths 解析。client 层读 import.meta.env.PUBLIC_API_BASE，
// 这里通过 test.env 注入固定值以便断言 URL 拼接。
export default defineConfig({
  // hot:false 关掉 Solid Fast-Refresh 注入（@solid-refresh）——它在 Vitest 的
  // serve 式环境下会生成非法的 'file:///@solid-refresh' 导致 transform 报错。
  plugins: [tsconfigPaths(), solid({ hot: false })],
  test: {
    environment: "jsdom",
    globals: false,
    setupFiles: ["./test/setup.ts"],
    include: ["test/**/*.test.{ts,tsx}"],
    env: {
      PUBLIC_API_BASE: "http://127.0.0.1:8917",
      API_INTERNAL_BASE: "http://127.0.0.1:8917",
    },
  },
});
