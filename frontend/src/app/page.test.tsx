import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import HomePage from "@/app/page";
import { PRODUCT_DESCRIPTION, PRODUCT_NAME } from "@/lib/site";

describe("HomePage", () => {
  it("shows the product name as the main heading", () => {
    render(<HomePage />);

    expect(screen.getByRole("heading", { level: 1, name: PRODUCT_NAME })).toBeInTheDocument();
    expect(PRODUCT_NAME).toBe("ClipForge AI");
  });

  it("shows a short product description", () => {
    render(<HomePage />);

    expect(screen.getByText(PRODUCT_DESCRIPTION)).toBeInTheDocument();
  });

  it("lists the planned workflow steps in order", () => {
    render(<HomePage />);

    const list = screen.getByRole("list");
    const steps = within(list)
      .getAllByRole("heading", { level: 3 })
      .map((heading) => heading.textContent);

    expect(steps).toEqual(["Upload", "Transcribe", "Find clips", "Reframe", "Caption", "Export"]);
  });

  it("renders no interactive controls", () => {
    render(<HomePage />);

    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });
});
