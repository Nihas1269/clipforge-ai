"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { Button } from "@/components/button";
import { Container } from "@/components/container";
import { useCurrentUser } from "@/components/session/session-provider";
import { StatusMessage } from "@/components/status-message";

export const LOGIN_PATH = "/login";

/**
 * Wraps pages that need a session. It re-checks `/users/me` when it mounts
 * (the session may have changed on /login) and renders nothing protected
 * until that check has finished. Without a session it redirects to /login.
 */
export function AuthenticatedLayout({ children }: Readonly<{ children: ReactNode }>) {
  const router = useRouter();
  const { user, loading, error, refresh } = useCurrentUser();
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    let active = true;
    void refresh().finally(() => {
      if (active) {
        setChecked(true);
      }
    });
    return () => {
      active = false;
    };
  }, [refresh]);

  const unauthenticated = checked && !loading && !user && !error;

  useEffect(() => {
    if (unauthenticated) {
      router.replace(LOGIN_PATH);
    }
  }, [unauthenticated, router]);

  if (checked && user) {
    return <>{children}</>;
  }

  let status: ReactNode;
  if (!checked || loading) {
    status = <StatusMessage variant="loading" title="Checking your session…" />;
  } else if (error) {
    status = (
      <StatusMessage
        variant="error"
        title="Could not check your session"
        description={error}
        action={
          <Button variant="secondary" onClick={() => void refresh()}>
            Try again
          </Button>
        }
      />
    );
  } else {
    status = <StatusMessage variant="loading" title="Redirecting to log in…" />;
  }

  return <Container className="py-16 sm:py-24">{status}</Container>;
}
