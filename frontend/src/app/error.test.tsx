import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import ErrorPage from "@/app/error";

describe("ErrorPage", () => {
  it("announces the error without exposing its details", () => {
    render(<ErrorPage error={new Error("internal database failure")} reset={() => {}} />);

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Something went wrong");
    expect(alert).not.toHaveTextContent("internal database failure");
  });

  it("calls reset when the user retries", () => {
    const reset = vi.fn();
    render(<ErrorPage error={new Error("boom")} reset={reset} />);

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(reset).toHaveBeenCalledTimes(1);
  });
});
