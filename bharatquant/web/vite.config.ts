import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    assetsDir: "assets",
    emptyOutDir: true,
    outDir: "../backend/src/bq/static",
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
});
