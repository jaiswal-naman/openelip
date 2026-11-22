import type { NextConfig } from "next";

// `next dev` has no reverse proxy in front of it, so with DEV_API_ORIGIN set it forwards /v1/* to the API.
// A production build never carries this rewrite: there the proxy routes /v1/* before a request reaches Next.
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
