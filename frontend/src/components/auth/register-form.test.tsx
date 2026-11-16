import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RegisterForm } from "@/components/auth/register-form";
import { ApiError } from "@/lib/api/client";

const mocks = vi.hoisted(() => ({
  register: vi.fn(),
  replace: vi.fn(),
  push: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mocks.replace, push: mocks.push }),
}));

vi.mock("@/lib/api/auth", () => ({
  register: mocks.register,
}));

function fillIn(email: string, password: string) {
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: email } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: password } });
}

function submit() {
  fireEvent.click(screen.getByRole("button", { name: "Create account" }));
}

beforeEach(() => {
  mocks.register.mockReset();
  mocks.replace.mockReset();
  mocks.push.mockReset();
});

describe("RegisterForm", () => {
  it("rejects a password shorter than 8 characters", () => {
    render(<RegisterForm />);

    fillIn("alice@example.com", "1234567");
    submit();

    expect(screen.getByText("Password must be at least 8 characters.")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("Password")).toHaveFocus();
    expect(mocks.register).not.toHaveBeenCalled();
  });

  it("rejects an invalid email", () => {
    render(<RegisterForm />);

    fillIn("alice@", "password123");
    submit();

    expect(
      screen.getByText("Enter a valid email address, like name@example.com."),
    ).toBeInTheDocument();
    expect(mocks.register).not.toHaveBeenCalled();
  });

  it("accepts a password of exactly 8 characters and redirects to login", async () => {
    mocks.register.mockResolvedValue({ id: "1", email: "alice@example.com" });
    render(<RegisterForm />);

    fillIn("alice@example.com", "12345678");
    submit();

    await waitFor(() => expect(mocks.push).toHaveBeenCalledWith("/login?registered=1"));
    expect(mocks.register).toHaveBeenCalledWith({
      email: "alice@example.com",
      password: "12345678",
    });
  });

  it("disables the submit button while the request is pending", async () => {
    mocks.register.mockReturnValue(new Promise(() => {}));
    render(<RegisterForm />);

    fillIn("alice@example.com", "password123");
    submit();

    expect(await screen.findByRole("button", { name: "Creating account…" })).toBeDisabled();
  });

  it("announces the API error message on failure", async () => {
    mocks.register.mockRejectedValue(
      new ApiError(409, "email_taken", "An account with this email already exists."),
    );
    render(<RegisterForm />);

    fillIn("alice@example.com", "password123");
    submit();

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "An account with this email already exists.",
      ),
    );
    expect(screen.getByRole("button", { name: "Create account" })).toBeEnabled();
    expect(mocks.push).not.toHaveBeenCalled();
  });
});
