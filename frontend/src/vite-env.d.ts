/// <reference types="vite/client" />

// 前端 API 契约（混合数据模式）：
//   PUBLIC_API_BASE  —— 浏览器直连 FastAPI 的地址（写操作 + 生成轮询），
//                       经 Vite 注入到客户端与 SSR，必须以 PUBLIC_ 前缀暴露。
//   API_INTERNAL_BASE —— SolidStart 服务端读代理（server$）访问 FastAPI 的地址，
//                       仅服务端可见，默认走同源/内网，规避浏览器 CORS。
interface ImportMetaEnv {
  readonly PUBLIC_API_BASE?: string;
  readonly API_INTERNAL_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
