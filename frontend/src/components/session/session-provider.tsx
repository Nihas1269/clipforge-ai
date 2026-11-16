"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import type { PublicUser } from "@/lib/api/auth";
import { ApiError, getErrorMessage } from "@/lib/api/client";
import { getCurrentUser } from "@/lib/api/users";

export interface SessionContextValue {
  /** The logged-in user, or `null` when there is no session (or it is not known yet). */
  user: PublicUser | null;
  /** `true` while `GET /users/me` is in flight, including the initial check. */
  loading: boolean;
  /** A non-401 failure of the last check (e.g. the API is unreachable), else `null`. */
  error: string | null;
  /** Re-checks the session; only the latest call's response is applied. */
  refresh: () => Promise<PublicUser | null>;
  /** Replaces the user after a successful profile update. */
  setUser: (user: PublicUser) => void;
  /** Forgets the user, e.g. after logout. Ignores any check still in flight. */
  clear: () => void;
}

const SessionContext = createContext<SessionContextValue | null>(null);

/**
 * Tracks the logged-in user with `GET /api/v1/users/me`. The session itself
 * is the backend's HttpOnly cookie; only the public user is kept in memory.
 */
export function SessionProvider({ children }: Readonly<{ children: ReactNode }>) {
  const [user, setUserState] = useState<PublicUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef<Promise<PublicUser | null> | null>(null);
  // Incremented by every refresh() and by clear(), so a stale response (for
  // example one started before login or logout) is never applied.
  const latest = useRef(0);

  const refresh = useCallback((): Promise<PublicUser | null> => {
    const id = ++latest.current;
    const isCurrent = () => latest.current === id;
    setLoading(true);

    const request = (async () => {
      try {
        const current = await getCurrentUser();
        if (!isCurrent()) {
          return null;
        }
        setUserState(current);
        setError(null);
        return current;
      } catch (caught) {
        if (!isCurrent()) {
          return null;
        }
        if (caught instanceof ApiError && caught.status === 401) {
          setUserState(null);
          setError(null);
          return null;
        }
        // Keep a known user on a transient failure instead of logging them out.
        setError(getErrorMessage(caught));
        return null;
      } finally {
        if (isCurrent()) {
          inFlight.current = null;
          setLoading(false);
        }
      }
    })();
    inFlight.current = request;
    return request;
  }, []);

  const setUser = useCallback((next: PublicUser) => {
    setUserState(next);
    setError(null);
  }, []);

  const clear = useCallback(() => {
    latest.current += 1;
    inFlight.current = null;
    setUserState(null);
    setError(null);
    setLoading(false);
  }, []);

  // Initial check. Child effects run first, so skip it when a protected
  // layout has already started one.
  useEffect(() => {
    if (!inFlight.current) {
      void refresh();
    }
  }, [refresh]);

  const value = useMemo(
    () => ({ user, loading, error, refresh, setUser, clear }),
    [user, loading, error, refresh, setUser, clear],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useCurrentUser(): SessionContextValue {
  const context = useContext(SessionContext);
  if (!context) {
    throw new Error("useCurrentUser must be used inside a SessionProvider.");
  }
  return context;
}
