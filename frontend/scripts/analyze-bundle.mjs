// 产物体积分析（本地，不部署）：读取 Vite manifest，沿「入口 → 静态依赖 → 动态依赖」
// 图求每个路由首屏需要下载的 JS/CSS 传输体积（raw + gzip），并单列四步面板的懒加载 chunk。
// 用法：node scripts/analyze-bundle.mjs （从 frontend/ 目录）
import { readFileSync, existsSync } from "node:fs";
import { gzipSync, brotliCompressSync } from "node:zlib";
import { fileURLToPath } from "node:url";
import { dirname, resolve, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..");
const publicDir = join(root, ".output", "public");
const manifestPath = join(publicDir, ".vite", "manifest.json");

if (!existsSync(manifestPath)) {
  console.error("未找到 manifest，请先执行 pnpm build。路径：" + manifestPath);
  process.exit(1);
}

const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));

function bytesOf(fileKey) {
  const entry = manifest[fileKey];
  if (!entry) return { raw: 0, gzip: 0, brotli: 0 };
  const abs = join(publicDir, entry.file);
  if (!existsSync(abs)) return { raw: 0, gzip: 0, brotli: 0 };
  const buf = readFileSync(abs);
  return {
    raw: buf.length,
    gzip: gzipSync(buf, { level: 9 }).length,
    brotli: brotliCompressSync(buf).length,
    css: entry.css ?? [],
  };
}

// 递归收集某 chunk 的静态 imports（去重），返回 chunk key 集合。
function closure(key, seen = new Set()) {
  if (seen.has(key)) return seen;
  seen.add(key);
  const entry = manifest[key];
  if (entry?.imports) for (const dep of entry.imports) closure(dep, seen);
  return seen;
}

function routePayload(entryKey) {
  const chunks = [...closure(entryKey)];
  let raw = 0;
  let gzip = 0;
  let brotli = 0;
  const cssKeys = [];
  for (const c of chunks) {
    const b = bytesOf(c);
    raw += b.raw;
    gzip += b.gzip;
    brotli += b.brotli;
    for (const cssFile of b.css ?? []) cssKeys.push(cssFile);
  }
  // CSS 单独算（manifest 里 css 是相对 public 的路径，非 chunk key）。
  let cssRaw = 0;
  let cssGzip = 0;
  for (const cssFile of cssKeys) {
    const abs = join(publicDir, cssFile);
    if (!existsSync(abs)) continue;
    const buf = readFileSync(abs);
    cssRaw += buf.length;
    cssGzip += gzipSync(buf, { level: 9 }).length;
  }
  return { chunks, raw, gzip, brotli, cssRaw, cssGzip };
}

const kb = (n) => (n / 1024).toFixed(1) + " KB";

const rows = [
  ["项目列表 /projects", "src/routes/projects/index.tsx?pick=default&pick=$css&lang.tsx"],
  ["项目详情重定向 /projects/[id]", "src/routes/projects/[id]/index.tsx?pick=default&pick=$css&lang.tsx"],
  ["分步主体 /projects/[id]/[step]", "src/routes/projects/[id]/[step].tsx?pick=default&pick=$css&lang.tsx"],
  ["客户端入口 entry-client", "src/entry-client.tsx"],
];

console.log("\n=== 首屏传输体积（入口 + 全部静态依赖，闭包去重） ===\n");
console.log(
  "路由".padEnd(34) +
    "JS raw".padStart(10) +
    "JS gzip".padStart(10) +
    "JS br".padStart(10) +
    "CSS gzip".padStart(10),
);
for (const [label, key] of rows) {
  const p = routePayload(key);
  console.log(
    label.padEnd(34) +
      kb(p.raw).padStart(10) +
      kb(p.gzip).padStart(10) +
      kb(p.brotli).padStart(10) +
      kb(p.cssGzip).padStart(10),
  );
}

console.log("\n=== 四步面板：懒加载动态 chunk（仅当进入该步才下载） ===\n");
const stepKeys = [
  "src/components/workspace/content-step.tsx",
  "src/components/workspace/visual-step.tsx",
  "src/components/workspace/audio-step.tsx",
  "src/components/workspace/output-step.tsx",
];
for (const key of stepKeys) {
  const b = bytesOf(key);
  const entry = manifest[key];
  console.log(
    (entry?.name ?? key).padEnd(16) +
      kb(b.raw).padStart(10) +
      kb(b.gzip).padStart(10) +
      kb(b.brotli).padStart(10),
  );
}

// 全量客户端产物总计。
console.log("\n=== 客户端产物总计（.output/public/_build/assets 全部 JS+CSS） ===\n");
let totalRaw = 0;
let totalGzip = 0;
for (const key of Object.keys(manifest)) {
  const entry = manifest[key];
  if (!entry?.file) continue;
  const abs = join(publicDir, entry.file);
  if (!existsSync(abs)) continue;
  const buf = readFileSync(abs);
  totalRaw += buf.length;
  totalGzip += gzipSync(buf, { level: 9 }).length;
}
console.log("全部 chunk raw : " + kb(totalRaw));
console.log("全部 chunk gzip: " + kb(totalGzip));
console.log("\n说明：route 面板相互共享 web/shared/toast/select 等基础 chunk（HTTP 缓存命中，不重复下载）。\n");
