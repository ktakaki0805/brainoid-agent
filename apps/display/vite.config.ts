import { defineConfig } from "vite";

export default defineConfig(({ mode }) => ({
  base: "./",
  build: { outDir: mode === "fake" ? "dist-fake" : "dist", sourcemap: false },
  server: { host: "127.0.0.1", strictPort: true },
  preview: { host: "127.0.0.1", strictPort: true },
}));
