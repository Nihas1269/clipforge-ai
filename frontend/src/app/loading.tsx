import { Container } from "@/components/container";
import { StatusMessage } from "@/components/status-message";

export default function Loading() {
  return (
    <Container className="py-16">
      <StatusMessage variant="loading" title="Loading…" />
    </Container>
  );
}
