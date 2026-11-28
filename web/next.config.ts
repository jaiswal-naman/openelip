import type { NextConfig } from "next";

// `next dev` has no reverse proxy in front of it, so with DEV_API_ORIGIN set it forwards /v1/* to the API.
// A production build never carries this rewrite: there the proxy routes /v1/* before a request reaches Next.
// The rewrite replaces the Host header with the API's own, so the Host check that guards the API's local mode (APP_MODE=local)
// cannot protect a `next dev` setup. Run local mode through Docker Compose, where the proxy passes the browser's Host on.
const devApiOrigin =
  process.env.NODE_ENV === "development" ? process.env.DEV_API_ORIGIN?.replace(/\/+$/, "") : undefined;

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  async rewrites() {
    return devApiOrigin ? [{ source: "/v1/:path*", destination: `${devApiOrigin}/v1/:path*` }] : [];
  },
};

export default nextConfig;
