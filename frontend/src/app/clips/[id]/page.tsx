import type { Metadata } from "next";

import { ClipEditor } from "@/components/clips/clip-editor";
import { Container } from "@/components/container";
import { PRODUCT_NAME } from "@/lib/site";

export const metadata: Metadata = {
  title: `Edit clip — ${PRODUCT_NAME}`,
};

/** The session is checked by `clips/layout.tsx`; all data is loaded by the authenticated API. */
export default async function ClipEditorPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <Container className="py-12 sm:py-16">
      <ClipEditor key={id} clipId={id} />
    </Container>
  );
}
