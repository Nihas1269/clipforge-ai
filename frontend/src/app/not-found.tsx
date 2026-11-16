import Link from "next/link";

import { Container } from "@/components/container";
import { StatusMessage } from "@/components/status-message";

export default function NotFound() {
  return (
    <Container className="py-16">
      <StatusMessage
        variant="empty"
        title="Page not found"
        description="The page you are looking for does not exist or has moved."
        action={
          <Link
            href="/"
            className="text-sm font-medium text-accent underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          >
            Go to the home page
          </Link>
        }
      />
    </Container>
  );
}
