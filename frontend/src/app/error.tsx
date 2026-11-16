"use client";

import { Button } from "@/components/button";
import { Container } from "@/components/container";
import { StatusMessage } from "@/components/status-message";

interface ErrorPageProps {
  error: Error & { digest?: string };
  reset: () => void;
}

// The error details are intentionally not rendered: they may contain internal
// information and are not actionable for the user.
export default function ErrorPage({ reset }: ErrorPageProps) {
  return (
    <Container className="py-16">
      <StatusMessage
        variant="error"
        title="Something went wrong"
        description="This page could not be displayed. Try again, and if the problem continues, reload the page."
        action={<Button onClick={() => reset()}>Try again</Button>}
      />
    </Container>
  );
}
