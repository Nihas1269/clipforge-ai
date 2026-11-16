import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import NotFound from "@/app/not-found";

describe("NotFound", () => {
  it("explains the page is missing and links home", () => {
    render(<NotFound />);

    expect(screen.getByRole("status")).toHaveTextContent("Page not found");
    expect(screen.getByRole("link", { name: "Go to the home page" })).toHaveAttribute("href", "/");
  });
});
