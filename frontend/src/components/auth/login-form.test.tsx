import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LoginForm } from "@/components/auth/login-form";
import { ApiError } from "@/lib/api/client";

const mocks = vi.hoisted(() => ({
  login: vi.fn(),
  replace: vi.fn(),
  push: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mocks.replace, push: mocks.push }),
}));

vi.mock("@/lib/api/auth", () => ({
  login: mocks.login,
}));

function fillIn(email: string, password: string) {
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: email } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: password } });
}

function submit() {
  fireEvent.click(screen.getByRole("button", { name: "Log in" }));
}

beforeEach(() => {
  mocks.login.mockReset();
  mocks.replace.mockReset();
  mocks.push.mockReset();
});

describe("LoginForm", () => {
  it("labels the inputs", () => {
    render(<LoginForm />);

    expect(screen.getByLabelText("Email")).toHaveAttribute("type", "email");
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
  });

  it("shows validation errors and does not call login", () => {
    render(<LoginForm />);

    submit();

    expect(screen.getByText("Enter your email address.")).toBeInTheDocument();
    expect(screen.getByText("Enter your password.")).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("Email")).toHaveAccessibleDescription("Enter your email address.");
    expect(screen.getByLabelText("Email")).toHaveFocus();
    expect(mocks.login).not.toHaveBeenCalled();
  });

  it("rejects an invalid email and a short password", () => {
    render(<LoginForm />);

    fillIn("not-an-email", "short");
    submit();

    expect(
      screen.getByText("Enter a valid email address, like name@example.com."),
    ).toBeInTheDocument();
    expect(screen.getByText("Password must be at least 8 characters.")).toBeInTheDocument();
    expect(mocks.login).not.toHaveBeenCalled();
  });

  it("calls login with the entered values and redirects to the dashboard", async () => {
    mocks.login.mockResolvedValue(undefined);
    render(<LoginForm />);

    fillIn(" alice@example.com ", "password123");
    submit();

    await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/dashboard"));
    expect(mocks.login).toHaveBeenCalledTimes(1);
    expect(mocks.login).toHaveBeenCalledWith({
      email: "alice@example.com",
      password: "password123",
    });
  });

  it("disables the submit button while the request is pending", async () => {
    mocks.login.mockReturnValue(new Promise(() => {}));
    render(<LoginForm />);

    fillIn("alice@example.com", "password123");
    submit();

    expect(await screen.findByRole("button", { name: "Logging in…" })).toBeDisabled();
    expect(mocks.login).toHaveBeenCalledTimes(1);
  });

  it("announces the API error message on failure and does not redirect", async () => {
    mocks.login.mockRejectedValue(
      new ApiError(401, "invalid_credentials", "Incorrect email or password."),
    );
    render(<LoginForm />);

    fillIn("alice@example.com", "wrong-password");
    submit();

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("Incorrect email or password."),
    );
    expect(screen.getByRole("button", { name: "Log in" })).toBeEnabled();
    expect(mocks.replace).not.toHaveBeenCalled();
    expect(mocks.push).not.toHaveBeenCalled();
  });

  it("shows a generic message for unexpected errors", async () => {
    mocks.login.mockRejectedValue(new Error("internal detail"));
    render(<LoginForm />);

    fillIn("alice@example.com", "password123");
    submit();

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("Something went wrong. Please try again."),
    );
    expect(screen.getByRole("alert")).not.toHaveTextContent("internal detail");
  });

  it("shows the registration success message", () => {
    render(<LoginForm registered />);

    expect(screen.getByRole("status")).toHaveTextContent("Account created. You can now log in.");
  });

  it("does not show the success message by default", () => {
    render(<LoginForm />);

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
