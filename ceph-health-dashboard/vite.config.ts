import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const projectDir = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [react()],
  server: { host: "0.0.0.0", port: 5174 },
  build: {
    outDir: resolve(projectDir, "../dashboard/static/ceph-health"),
    emptyOutDir: true,
    cssCodeSplit: false,
    rollupOptions: {
      input: resolve(projectDir, "src/main.tsx"),
      output: {
        entryFileNames: "app.js",
        chunkFileNames: "chunks/[name].js",
        assetFileNames: (assetInfo) => assetInfo.name?.endsWith(".css") ? "style.css" : "assets/[name][extname]",
        inlineDynamicImports: true
      }
    }
  }
});
