import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusMessage, type StatusVariant } from "@/components/status-message";

describe("StatusMessage", () => {
  it("uses role=alert for errors", () => {
    render(<StatusMessage variant="error" title="Upload failed" />);

    expect(screen.getByRole("alert")).toHaveTextContent("Upload failed");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it.each<StatusVariant>(["loading", "empty", "success"])(
    "uses role=status for the %s state",
    (variant) => {
      render(<StatusMessage variant={variant} title="Message" />);

      const region = screen.getByRole("status");
      expect(region).toHaveAttribute("data-variant", variant);
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    },
  );

  it("renders the optional description and action", () => {
    render(
      <StatusMessage
        variant="empty"
        title="No projects yet"
        description="Upload a video to get started."
        action={<button type="button">Upload</button>}
      />,
    );

    expect(screen.getByText("Upload a video to get started.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Upload" })).toBeInTheDocument();
  });

  it("omits the description when none is given", () => {
    const { container } = render(<StatusMessage variant="success" title="Saved" />);

    expect(container.querySelectorAll("p")).toHaveLength(1);
  });
});
