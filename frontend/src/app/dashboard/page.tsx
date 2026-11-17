import type { Metadata } from "next";

import { Container } from "@/components/container";
import { Dashboard } from "@/components/dashboard/dashboard";
import { PRODUCT_NAME } from "@/lib/site";

export const metadata: Metadata = {
  title: `Dashboard — ${PRODUCT_NAME}`,
};

/** The session is checked by `layout.tsx`. */
export default function DashboardPage() {
  return (
    <Container className="py-16 sm:py-24">
      <Dashboard />
    </Container>
  );
}
