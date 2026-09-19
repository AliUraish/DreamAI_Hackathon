import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // The Chowkidaar backend (FastAPI). The app has no other data source.
    proxy: { "/api": "http://localhost:8000" },
  },
});
