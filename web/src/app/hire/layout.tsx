import type { Metadata } from "next";
import type { ReactNode } from "react";
import { HiringShell } from "@/features/hiring/HiringShell";

export const metadata: Metadata = { title: "Hiring desk" };

/** The hiring desk frame. Each page checks who may see it (guardHiringPage); the frame shows the org name, the sections and the pause badge. */
export default function HireLayout({ children }: { children: ReactNode }) {
  return (
    <div className="container page">
      <HiringShell>{children}</HiringShell>
    </div>
  );
}
