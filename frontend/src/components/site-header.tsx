import Link from "next/link";
import type { ReactNode } from "react";

import { Container } from "@/components/container";
import { PRODUCT_NAME } from "@/lib/site";

export interface SiteHeaderProps {
  /** Right-aligned controls, such as the user menu. */
  actions?: ReactNode;
}

export function SiteHeader({ actions }: SiteHeaderProps) {
  return (
    <header className="border-b border-border bg-surface">
      <Container className="flex h-14 items-center justify-between gap-4">
        <Link
          href="/"
          className="rounded-sm text-base font-semibold tracking-tight text-fg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        >
          {PRODUCT_NAME}
        </Link>
        {actions}
      </Container>
    </header>
  );
}
