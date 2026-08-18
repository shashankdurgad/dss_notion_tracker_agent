import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Proxy API calls to FastAPI so the session cookie stays same-origin
    // in development — avoids third-party-cookie blocking in browsers.
    proxy: {
      "/auth": { target: "http://localhost:8000", changeOrigin: true },
      "/chat": { target: "http://localhost:8000", changeOrigin: true },
      "/approve": { target: "http://localhost:8000", changeOrigin: true },
      "/reset": { target: "http://localhost:8000", changeOrigin: true },
      "/health": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
});
