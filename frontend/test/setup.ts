import { afterEach } from "vitest";
import { cleanup } from "@solidjs/testing-library";

// 每个用例后卸载 Solid 根，避免跨用例的响应式/dispose 泄漏。
afterEach(() => cleanup());
