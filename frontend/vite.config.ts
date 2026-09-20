import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Proxy API and tile requests to the backend so the frontend never hardcodes a host.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/health": "http://localhost:8000",
      "/tiles": "http://localhost:8000",
    },
  },
});
