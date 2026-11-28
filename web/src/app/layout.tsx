import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import type { ReactNode } from "react";
import { Providers } from "@/components/shell/Providers";
import { SiteFooter } from "@/components/shell/SiteFooter";
import { SiteHeader } from "@/components/shell/SiteHeader";
import { authKeys, meQuery } from "@/features/auth/queries";
import { hasSessionCookie, serverApi } from "@/lib/api/server";
import { PRODUCT_NAME } from "@/lib/product";
import { makeQueryClient } from "@/lib/query";
import { THEME_SCRIPT } from "@/lib/themeScript";
import "@/styles/tokens.css";
import "@/styles/global.css";

// next/font downloads these at build time and serves them from this app: no request goes to a font service at run time.
// Geist for reading and headings, Geist Mono for small numerals and data (ids, times, amounts).
const geist = Geist({ subsets: ["latin"], variable: "--font-geist", display: "swap" });
const geistMono = Geist_Mono({ subsets: ["latin"], variable: "--font-geist-mono", display: "swap" });

export const metadata: Metadata = {
  title: { default: PRODUCT_NAME, template: `%s, ${PRODUCT_NAME}` },
  description: "Search open jobs from public company boards. An open-source career agent, still being built.",
};

export const viewport: Viewport = {
  colorScheme: "light dark",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#fafafa" },
    { media: "(prefers-color-scheme: dark)", color: "#0a0a0a" },
  ],
};

export default async function RootLayout({ children }: { children: ReactNode }) {
  // Who is signed in is asked here, once, so the header is right on first paint. With no session cookie nobody is, and the
  // API is not called. If the API cannot answer, nothing is handed over and the browser asks for itself.
  const queryClient = makeQueryClient();
  if (await hasSessionCookie()) await queryClient.prefetchQuery(meQuery(await serverApi()));
  else queryClient.setQueryData(authKeys.me, null);

  return (
    // suppressHydrationWarning: the script below sets data-theme on <html> before React sees it
    <html lang="en" className={`${geist.variable} ${geistMono.variable}`} suppressHydrationWarning>
      <head>
        <script>{THEME_SCRIPT}</script>
      </head>
      <body>
        <Providers>
          <HydrationBoundary state={dehydrate(queryClient)}>
            <div className="shell">
              <SiteHeader />
              <div className="sheet">
                <main id="main" tabIndex={-1}>
                  {children}
                </main>
                <SiteFooter />
              </div>
            </div>
          </HydrationBoundary>
        </Providers>
      </body>
    </html>
  );
}
