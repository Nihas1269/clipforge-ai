"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/button";
import { LOGIN_PATH } from "@/components/session/authenticated-layout";
import { useCurrentUser } from "@/components/session/session-provider";
import { logout } from "@/lib/api/auth";
import { getErrorMessage } from "@/lib/api/client";

/** The logged-in user's name (or email), a profile link, and the logout button. */
export function UserMenu() {
  const router = useRouter();
  const { user, clear } = useCurrentUser();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleLogout() {
    if (pending) {
      return;
    }
    setPending(true);
    setError(null);
    try {
      await logout();
    } catch (caught) {
      // The cookie may still be set, so do not pretend the user is logged out.
      setError(getErrorMessage(caught));
      setPending(false);
      return;
    }
    clear();
    setPending(false);
    router.replace(LOGIN_PATH);
  }

  if (!user) {
    return null;
  }

  const displayName = user.full_name?.trim() || user.email;

  return (
    <div className="flex items-center gap-3">
      {error ? (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      ) : null}
      <Link
        href="/settings/profile"
        className="max-w-48 truncate rounded-sm text-sm text-fg-muted hover:text-fg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        title={displayName}
      >
        {displayName}
      </Link>
      <Button variant="secondary" onClick={() => void handleLogout()} disabled={pending}>
        {pending ? "Logging out…" : "Log out"}
      </Button>
    </div>
  );
}
