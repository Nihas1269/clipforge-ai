import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AuthenticatedLayout } from "@/components/session/authenticated-layout";
import { SessionProvider } from "@/components/session/session-provider";
import { ApiError } from "@/lib/api/client";

const mocks = vi.hoisted(() => {
  const replace = vi.fn();
  const push = vi.fn();
  return { getCurrentUser: vi.fn(), replace, push, router: { replace, push } };
});

vi.mock("next/navigation", () => ({
  useRouter: () => mocks.router,
}));

vi.mock("@/lib/api/users", () => ({
  getCurrentUser: mocks.getCurrentUser,
}));

const user = {
  id: "6f1c1a52-1d1f-4c3e-9f55-0d0b8f0d2a10",
  email: "alice@example.com",
  full_name: "Alice Example",
  is_active: true,
  email_verified: false,
  created_at: "2026-01-01T00:00:00Z",
};

function unauthorized() {
  return new ApiError(401, "not_authenticated", "Not authenticated");
}

function renderProtected() {
  return render(
    <SessionProvider>
      <AuthenticatedLayout>
        <p>Protected content</p>
      </AuthenticatedLayout>
    </SessionProvider>,
  );
}

beforeEach(() => {
  mocks.getCurrentUser.mockReset();
  mocks.replace.mockReset();
  mocks.push.mockReset();
});

describe("AuthenticatedLayout", () => {
  it("redirects to /login when /users/me returns 401 and never shows the content", async () => {
    mocks.getCurrentUser.mockRejectedValue(unauthorized());
    renderProtected();

    expect(screen.queryByText("Protected content")).not.toBeInTheDocument();
    await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/login"));
    expect(screen.queryByText("Protected content")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Redirecting to log in…");
  });

  it("hides the content while the session check is pending", () => {
    mocks.getCurrentUser.mockReturnValue(new Promise(() => {}));
    renderProtected();

    expect(screen.getByRole("status")).toHaveTextContent("Checking your session…");
    expect(screen.queryByText("Protected content")).not.toBeInTheDocument();
    expect(mocks.replace).not.toHaveBeenCalled();
  });

  it("renders the content for an authenticated user", async () => {
    mocks.getCurrentUser.mockResolvedValue(user);
    renderProtected();

    expect(await screen.findByText("Protected content")).toBeInTheDocument();
    expect(mocks.replace).not.toHaveBeenCalled();
    // The provider and the layout share one request.
    expect(mocks.getCurrentUser).toHaveBeenCalledTimes(1);
  });

  it("shows an error with a retry instead of redirecting when the API is unreachable", async () => {
    mocks.getCurrentUser.mockRejectedValueOnce(
      new ApiError(0, "network_error", "Could not reach the server."),
    );
    renderProtected();

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not reach the server.");
    expect(mocks.replace).not.toHaveBeenCalled();
    expect(screen.queryByText("Protected content")).not.toBeInTheDocument();

    mocks.getCurrentUser.mockResolvedValueOnce(user);
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByText("Protected content")).toBeInTheDocument();
    expect(mocks.replace).not.toHaveBeenCalled();
  });

  it("re-checks the session on mount, e.g. after logging in", async () => {
    mocks.getCurrentUser.mockRejectedValueOnce(unauthorized()).mockResolvedValueOnce(user);
    const { rerender } = render(
      <SessionProvider>
        <p>Login page</p>
      </SessionProvider>,
    );
    await waitFor(() => expect(mocks.getCurrentUser).toHaveBeenCalledTimes(1));

    rerender(
      <SessionProvider>
        <AuthenticatedLayout>
          <p>Protected content</p>
        </AuthenticatedLayout>
      </SessionProvider>,
    );

    expect(await screen.findByText("Protected content")).toBeInTheDocument();
    expect(mocks.getCurrentUser).toHaveBeenCalledTimes(2);
    expect(mocks.replace).not.toHaveBeenCalled();
  });
});
