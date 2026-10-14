import js from "@eslint/js";
import tseslint from "typescript-eslint";
import solid from "eslint-plugin-solid/configs/typescript";
import prettier from "eslint-config-prettier";

// ESLint v9 扁平配置：TS strict + Solid 细粒度响应式规则（如 no-react-destructure
// 对应到 solid 的 props 访问约束），并关闭与 Prettier 冲突的样式类规则。
export default tseslint.config(
  { ignores: ["dist/", ".output/", "node_modules/", ".vinxi/"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    ...solid,
    languageOptions: {
      parser: tseslint.parser,
      parserOptions: { project: "./tsconfig.json" },
    },
  },
  prettier,
  {
    rules: {
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
);
