import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PasswordForm } from "@/components/profile/password-form";
import { ApiError } from "@/lib/api/client";

const mocks = vi.hoisted(() => ({
  changePassword: vi.fn(),
}));

vi.mock("@/lib/api/users", () => ({
  changePassword: mocks.changePassword,
}));

function fillIn(current: string, next: string, confirmation: string) {
  fireEvent.change(screen.getByLabelText("Current password"), { target: { value: current } });
  fireEvent.change(screen.getByLabelText("New password"), { target: { value: next } });
  fireEvent.change(screen.getByLabelText("Confirm new password"), {
    target: { value: confirmation },
  });
}

function submit() {
  fireEvent.click(screen.getByRole("button", { name: "Change password" }));
}

beforeEach(() => {
  mocks.changePassword.mockReset();
});

describe("PasswordForm", () => {
  it("shows the backend error for a wrong current password", async () => {
    mocks.changePassword.mockRejectedValue(
      new ApiError(400, "invalid_current_password", "The current password is incorrect."),
    );
    render(<PasswordForm />);

    fillIn("wrong-password", "new-password-1", "new-password-1");
    submit();

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("The current password is incorrect."),
    );
    expect(mocks.changePassword).toHaveBeenCalledWith({
      current_password: "wrong-password",
      new_password: "new-password-1",
    });
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    expect(screen.getByRole("button", { name: "Change password" })).toBeEnabled();
  });

  it("changes the password, shows success, and clears the fields", async () => {
    mocks.changePassword.mockResolvedValue(undefined);
    render(<PasswordForm />);

    fillIn("old-password", "new-password-1", "new-password-1");
    submit();

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("Your password has been changed."),
    );
    expect(mocks.changePassword).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("Current password")).toHaveValue("");
    expect(screen.getByLabelText("New password")).toHaveValue("");
    expect(screen.getByLabelText("Confirm new password")).toHaveValue("");
  });

  it("validates the fields without calling the API", () => {
    render(<PasswordForm />);

    submit();

    expect(screen.getByText("Enter your current password.")).toBeInTheDocument();
    expect(screen.getByText("Enter a new password.")).toBeInTheDocument();
    expect(screen.getByText("Confirm your new password.")).toBeInTheDocument();
    expect(screen.getByLabelText("Current password")).toHaveFocus();
    expect(mocks.changePassword).not.toHaveBeenCalled();
  });

  it("rejects a short new password and a mismatched confirmation", () => {
    render(<PasswordForm />);

    fillIn("old-password", "short", "different");
    submit();

    expect(screen.getByText("Password must be at least 8 characters.")).toBeInTheDocument();
    expect(screen.getByText("The passwords do not match.")).toBeInTheDocument();
    expect(screen.getByLabelText("New password")).toHaveFocus();
    expect(mocks.changePassword).not.toHaveBeenCalled();
  });

  it("disables the submit button while the request is pending", async () => {
    mocks.changePassword.mockReturnValue(new Promise(() => {}));
    render(<PasswordForm />);

    fillIn("old-password", "new-password-1", "new-password-1");
    submit();

    expect(await screen.findByRole("button", { name: "Changing password…" })).toBeDisabled();
    expect(mocks.changePassword).toHaveBeenCalledTimes(1);
  });
});
