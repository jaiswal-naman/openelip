import type { Metadata, Viewport } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import type { ReactNode } from "react";
import { Providers } from "@/components/shell/Providers";
import { SiteFooter } from "@/components/shell/SiteFooter";
import { SiteHeader } from "@/components/shell/SiteHeader";
import { PRODUCT_NAME } from "@/lib/product";
import { THEME_SCRIPT } from "@/lib/themeScript";
import "@/styles/tokens.css";
import "@/styles/global.css";

// next/font downloads these at build time and serves them from this app: no request goes to a font service at run time.
const inter = Inter({ subsets: ["latin"], variable: "--font-inter", display: "swap" });
const jetbrainsMono = JetBrains_Mono({ subsets: ["latin"], variable: "--font-jetbrains-mono", display: "swap" });

export const metadata: Metadata = {
  title: { default: PRODUCT_NAME, template: `%s, ${PRODUCT_NAME}` },
  description: "Search open jobs from public company boards. An open-source career agent, still being built.",
};

export const viewport: Viewport = {
  colorScheme: "light dark",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f6f3ec" },
    { media: "(prefers-color-scheme: dark)", color: "#0d1015" },
  ],
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    // suppressHydrationWarning: the script below sets data-theme on <html> before React sees it
    <html lang="en" className={`${inter.variable} ${jetbrainsMono.variable}`} suppressHydrationWarning>
      <head>
        <script>{THEME_SCRIPT}</script>
      </head>
      <body>
        <Providers>
          <SiteHeader />
          <main id="main" tabIndex={-1}>
            {children}
          </main>
          <SiteFooter />
        </Providers>
      </body>
    </html>
  );
}
