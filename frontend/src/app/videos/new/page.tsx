import type { Metadata } from "next";

import { Container } from "@/components/container";
import { VideoUploadForm } from "@/components/videos/video-upload-form";
import { PRODUCT_NAME } from "@/lib/site";

export const metadata: Metadata = {
  title: `Upload a video — ${PRODUCT_NAME}`,
};

/** The session is checked by `videos/layout.tsx`. */
export default function NewVideoPage() {
  return (
    <Container className="py-16 sm:py-24">
      <div className="mx-auto max-w-xl">
        <VideoUploadForm />
      </div>
    </Container>
  );
}
