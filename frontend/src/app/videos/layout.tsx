import type { ReactNode } from "react";

import { AuthenticatedLayout } from "@/components/session/authenticated-layout";

export default function VideosLayout({ children }: Readonly<{ children: ReactNode }>) {
  return <AuthenticatedLayout>{children}</AuthenticatedLayout>;
}
