import Link from "next/link";

import { Container } from "@/components/container";
import { PRODUCT_NAME } from "@/lib/site";

/** Placeholder header: product wordmark only until navigation exists. */
export function SiteHeader() {
  return (
    <header className="border-b border-border bg-surface">
      <Container className="flex h-14 items-center">
        <Link
          href="/"
          className="rounded-sm text-base font-semibold tracking-tight text-fg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        >
          {PRODUCT_NAME}
        </Link>
      </Container>
    </header>
  );
}
