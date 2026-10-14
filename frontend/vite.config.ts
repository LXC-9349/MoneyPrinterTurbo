import { defineConfig } from "vite";
import { nitro } from "nitro/vite";
import { solidStart } from "@solidjs/start/config";
import tailwindcss from "@tailwindcss/vite";
import tsConfigPaths from "vite-tsconfig-paths";

// SolidStart v2 = Vite 原生：solidStart() 负责 SSR/文件路由/流式，
// nitro() 负责服务端产物（Nitro 3），tailwindcss() 提供 v4 零运行时样式，
// vite-tsconfig-paths() 让 tsconfig 里的 ~/* 别名在构建期生效。
// 前端 dev 源固定 3000，与后端开发期 CORS_ALLOWED_ORIGINS=http://localhost:3000 对齐。
// 产物体积分析不靠 rollup 插件（SolidStart 多环境构建不注入），而是构建后
// 沿 Vite manifest 闭包统计，见 scripts/analyze-bundle.mjs。
export default defineConfig({
  plugins: [tsConfigPaths(), solidStart(), tailwindcss(), nitro()],
  // 暴露 PUBLIC_ 前缀的环境变量给浏览器（客户端直连写/轮询用的 PUBLIC_API_BASE）；
  // Vite 默认只暴露 VITE_ 前缀，SolidStart 不会自动追加，故在此显式声明。
  envPrefix: ["VITE_", "PUBLIC_"],
  server: { port: 3000, strictPort: true },
  preview: { port: 3000, strictPort: true },
});
