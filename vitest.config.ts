import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

// Without a local config, vitest walks up and loads the unrelated
// vite.config.ts in the parent checkout — pin the root here.
export default defineConfig({
  resolve: {
    alias: {
      // Both JSX-runtime entries come FIRST: a string alias key is a prefix
      // match in Vite, so a bare `react` alias would rewrite
      // `react/jsx-dev-runtime` to `<react-stub>/jsx-dev-runtime` and fail to
      // resolve. tsconfig sets `"jsx": "react-jsx"`, so any `.tsx` module
      // under test imports one of these.
      "react/jsx-dev-runtime": fileURLToPath(
        new URL(
          "./src/test-support/react-jsx-runtime-stub.ts",
          import.meta.url,
        ),
      ),
      "react/jsx-runtime": fileURLToPath(
        new URL(
          "./src/test-support/react-jsx-runtime-stub.ts",
          import.meta.url,
        ),
      ),
      // React is peer-provided by the Steam webview (SP_REACT) and not
      // installed in node_modules — resolve it to a minimal stub so
      // steam-bridge modules can be imported under test.
      react: fileURLToPath(
        new URL("./src/test-support/react-stub.ts", import.meta.url),
      ),
    },
  },
  test: {
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
