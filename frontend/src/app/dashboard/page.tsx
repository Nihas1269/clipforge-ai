import type { Metadata } from "next";

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
    </Container>
  );
}
