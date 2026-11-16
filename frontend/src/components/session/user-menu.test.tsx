import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SessionProvider } from "@/components/session/session-provider";
import { UserMenu } from "@/components/session/user-menu";
import { SiteHeader } from "@/components/site-header";
import { ApiError } from "@/lib/api/client";

const mocks = vi.hoisted(() => {
  const replace = vi.fn();
  const push = vi.fn();
  return { getCurrentUser: vi.fn(), logout: vi.fn(), replace, push, router: { replace, push } };
});

vi.mock("next/navigation", () => ({
  useRouter: () => mocks.router,
}));

vi.mock("@/lib/api/users", () => ({
  getCurrentUser: mocks.getCurrentUser,
}));

vi.mock("@/lib/api/auth", () => ({
  logout: mocks.logout,
}));

const user = {
  id: "6f1c1a52-1d1f-4c3e-9f55-0d0b8f0d2a10",
  email: "alice@example.com",
  full_name: "Alice Example",
  is_active: true,
  email_verified: false,
  created_at: "2026-01-01T00:00:00Z",
};

function renderHeader() {
  return render(
    <SessionProvider>
      <SiteHeader actions={<UserMenu />} />
    </SessionProvider>,
  );
}

beforeEach(() => {
  mocks.getCurrentUser.mockReset();
  mocks.logout.mockReset();
  mocks.replace.mockReset();
  mocks.push.mockReset();
});

describe("UserMenu in the header", () => {
  it("shows the user's name and a logout button when authenticated", async () => {
    mocks.getCurrentUser.mockResolvedValue(user);
    renderHeader();

    const header = screen.getByRole("banner");
    expect(await within(header).findByRole("link", { name: "Alice Example" })).toHaveAttribute(
      "href",
      "/settings/profile",
    );
    expect(within(header).getByRole("button", { name: "Log out" })).toBeEnabled();
  });

  it("falls back to the email when the user has no name", async () => {
    mocks.getCurrentUser.mockResolvedValue({ ...user, full_name: null });
    renderHeader();

    expect(await screen.findByRole("link", { name: "alice@example.com" })).toBeInTheDocument();
  });

  it("renders no user controls without a session", async () => {
    mocks.getCurrentUser.mockRejectedValue(new ApiError(401, "not_authenticated", "Nope"));
    renderHeader();

    await waitFor(() => expect(mocks.getCurrentUser).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Log out" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "ClipForge AI" })).toBeInTheDocument();
  });

  it("logs out through the API, clears the user, and redirects to /login", async () => {
    mocks.getCurrentUser.mockResolvedValue(user);
    mocks.logout.mockResolvedValue(undefined);
    renderHeader();

    fireEvent.click(await screen.findByRole("button", { name: "Log out" }));

    await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/login"));
    expect(mocks.logout).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Alice Example")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Log out" })).not.toBeInTheDocument();
  });

  it("keeps the session and shows an error when logout fails", async () => {
    mocks.getCurrentUser.mockResolvedValue(user);
    mocks.logout.mockRejectedValue(
      new ApiError(0, "network_error", "Could not reach the server."),
    );
    renderHeader();

    fireEvent.click(await screen.findByRole("button", { name: "Log out" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not reach the server.");
    expect(mocks.replace).not.toHaveBeenCalled();
    expect(screen.getByText("Alice Example")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Log out" })).toBeEnabled();
  });
});
