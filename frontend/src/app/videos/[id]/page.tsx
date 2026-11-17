import type { Metadata } from "next";

import { Container } from "@/components/container";
import { VideoDetail } from "@/components/videos/video-detail";
import { PRODUCT_NAME } from "@/lib/site";

export const metadata: Metadata = {
  title: `Video — ${PRODUCT_NAME}`,
};

/** The session is checked by `videos/layout.tsx`; all data is loaded by the authenticated API. */
export default async function VideoDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <Container className="py-12 sm:py-16">
      <VideoDetail key={id} videoId={id} />
    </Container>
  );
}
