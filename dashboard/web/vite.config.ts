import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Proxies /api to the local Functions host (`func start`, port 7071 — Azure
// Functions serves HTTP-triggered routes under /api by default) so the browser
// never needs CORS or a hardcoded API origin — see functions_api/README.md.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:7071",
    },
  },
});
