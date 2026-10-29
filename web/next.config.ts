import type { NextConfig } from "next";

// Fully client-side: the page talks to the lab API from the browser, so it
// exports to static files that Vercel or the lab server's Caddy can serve.
const nextConfig: NextConfig = {
  output: "export",
};

export default nextConfig;
