import type { Metadata } from "next";
import Link from "next/link";

import { Container } from "@/components/container";
import { PRODUCT_NAME } from "@/lib/site";

export const metadata: Metadata = {
  title: `Dashboard — ${PRODUCT_NAME}`,
};

/** Placeholder until the dashboard is built; the session is checked by `layout.tsx`. */
export default function DashboardPage() {
  return (
    <Container className="py-16 sm:py-24">
      <h1 className="text-3xl font-semibold tracking-tight text-fg">Dashboard</h1>
      <p className="mt-4 text-base text-fg-muted">
        You are logged in. Your videos and clips will appear here.
      </p>
      <p className="mt-6">
        <Link
          href="/videos/new"
          className="text-sm font-medium text-accent underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        >
          Upload a video
        </Link>
      </p>
    </Container>
  );
}
