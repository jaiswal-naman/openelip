import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

export default defineConfig([
  ...nextVitals,
  ...nextTypescript,
  // Job text and every other string from the API is shown through React's escaping only (threat T-01-23).
  { rules: { "react/no-danger": "error" } },
  globalIgnores([".next/**", "out/**", "build/**", "coverage/**", "playwright-report/**", "test-results/**", "next-env.d.ts"]),
]);
