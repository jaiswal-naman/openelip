import Link from "next/link";
import type { ReactNode } from "react";
import { type ButtonSize, buttonClass, type ButtonVariant } from "./buttonStyles";

/** A link to another page of this app that looks like a button. */
export function LinkButton({
  href,
  variant,
  size,
  children,
}: {
  href: string;
  variant?: ButtonVariant;
  size?: ButtonSize;
  children: ReactNode;
}) {
  return (
    <Link href={href} className={buttonClass(variant, size)}>
      {children}
    </Link>
  );
}
