import type { Metadata } from "next";
import Link from "next/link";

import { Container } from "@/components/container";
import { PRODUCT_NAME } from "@/lib/site";

export const metadata: Metadata = {
  title: `Video — ${PRODUCT_NAME}`,
};

/** Placeholder until the video detail page is built (CF-030); the session is checked by `videos/layout.tsx`. */
export default function VideoDetailPage() {
  return (
    <Container className="py-16 sm:py-24">
      <h1 className="text-3xl font-semibold tracking-tight text-fg">Video uploaded</h1>
      <p className="mt-4 text-base text-fg-muted">
        Your video has been uploaded. Its details and processing status will appear here.
      </p>
      <p className="mt-6">
        <Link
          href="/videos/new"
          className="text-sm font-medium text-accent underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        >
          Upload another video
        </Link>
      </p>
    </Container>
  );
}
