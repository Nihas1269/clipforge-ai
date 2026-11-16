import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProfileForm } from "@/components/profile/profile-form";
import { AuthenticatedLayout } from "@/components/session/authenticated-layout";
import { SessionProvider } from "@/components/session/session-provider";
import { UserMenu } from "@/components/session/user-menu";
import { ApiError } from "@/lib/api/client";

const mocks = vi.hoisted(() => {
  const replace = vi.fn();
  const push = vi.fn();
  return {
    getCurrentUser: vi.fn(),
    updateProfile: vi.fn(),
    replace,
    push,
    router: { replace, push },
  };
});

vi.mock("next/navigation", () => ({
  useRouter: () => mocks.router,
}));

vi.mock("@/lib/api/users", () => ({
  getCurrentUser: mocks.getCurrentUser,
  updateProfile: mocks.updateProfile,
}));

vi.mock("@/lib/api/auth", () => ({
  logout: vi.fn(),
}));

const user = {
  id: "6f1c1a52-1d1f-4c3e-9f55-0d0b8f0d2a10",
  email: "alice@example.com",
  full_name: "Alice",
  is_active: true,
  email_verified: false,
  created_at: "2026-01-01T00:00:00Z",
};

async function renderForm() {
  render(
    <SessionProvider>
      <UserMenu />
      <AuthenticatedLayout>
        <ProfileForm />
      </AuthenticatedLayout>
    </SessionProvider>,
  );
  return within(await screen.findByRole("form", { name: "Personal details" }));
}

function save() {
  fireEvent.click(screen.getByRole("button", { name: "Save profile" }));
}

beforeEach(() => {
  mocks.getCurrentUser.mockReset();
  mocks.updateProfile.mockReset();
  mocks.replace.mockReset();
  mocks.push.mockReset();
  mocks.getCurrentUser.mockResolvedValue(user);
});

describe("ProfileForm", () => {
  it("prefills the current name and shows the email read-only", async () => {
    const form = await renderForm();

    expect(form.getByLabelText("Full name")).toHaveValue("Alice");
    expect(form.getByLabelText("Email")).toHaveValue("alice@example.com");
    expect(form.getByLabelText("Email")).toHaveAttribute("readonly");
  });

  it("submits the trimmed name, shows success, and updates the header", async () => {
    mocks.updateProfile.mockResolvedValue({ ...user, full_name: "Alice Smith" });
    const form = await renderForm();

    fireEvent.change(form.getByLabelText("Full name"), { target: { value: "  Alice Smith " } });
    save();

    await waitFor(() =>
      expect(form.getByRole("status")).toHaveTextContent("Your profile has been updated."),
    );
    expect(mocks.updateProfile).toHaveBeenCalledTimes(1);
    expect(mocks.updateProfile).toHaveBeenCalledWith({ full_name: "Alice Smith" });
    expect(form.getByLabelText("Full name")).toHaveValue("Alice Smith");
    expect(screen.getByRole("link", { name: "Alice Smith" })).toBeInTheDocument();
  });

  it("clears the name when submitted blank", async () => {
    mocks.updateProfile.mockResolvedValue({ ...user, full_name: null });
    const form = await renderForm();

    fireEvent.change(form.getByLabelText("Full name"), { target: { value: "   " } });
    save();

    await waitFor(() => expect(mocks.updateProfile).toHaveBeenCalledWith({ full_name: null }));
  });

  it("rejects a name over 255 characters without calling the API", async () => {
    const form = await renderForm();

    fireEvent.change(form.getByLabelText("Full name"), { target: { value: "a".repeat(256) } });
    save();

    expect(form.getByText("Name must be at most 255 characters.")).toBeInTheDocument();
    expect(form.getByLabelText("Full name")).toHaveAttribute("aria-invalid", "true");
    expect(mocks.updateProfile).not.toHaveBeenCalled();
  });

  it("shows the API error and no success message on failure", async () => {
    mocks.updateProfile.mockRejectedValue(
      new ApiError(422, "validation_error", "The request is invalid."),
    );
    const form = await renderForm();

    fireEvent.change(form.getByLabelText("Full name"), { target: { value: "Alice Smith" } });
    save();

    await waitFor(() =>
      expect(form.getByRole("alert")).toHaveTextContent("The request is invalid."),
    );
    expect(form.getByRole("status")).toBeEmptyDOMElement();
    expect(form.getByRole("button", { name: "Save profile" })).toBeEnabled();
  });
});
