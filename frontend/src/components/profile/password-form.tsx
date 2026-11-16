"use client";

import { useRef, useState, type FormEvent } from "react";

import { Button } from "@/components/button";
import { TextField } from "@/components/text-field";
import { getErrorMessage } from "@/lib/api/client";
import { changePassword } from "@/lib/api/users";
import {
  validateCurrentPassword,
  validateNewPassword,
  validatePasswordConfirmation,
} from "@/lib/validation/profile";

interface FieldErrors {
  current?: string;
  next?: string;
  confirmation?: string;
}

/**
 * Changes the password. The backend issues a fresh session cookie, so the
 * user stays logged in here while other sessions are signed out.
 */
export function PasswordForm() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [errors, setErrors] = useState<FieldErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const currentRef = useRef<HTMLInputElement>(null);
  const nextRef = useRef<HTMLInputElement>(null);
  const confirmationRef = useRef<HTMLInputElement>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) {
      return;
    }

    const nextErrors: FieldErrors = {
      current: validateCurrentPassword(current),
      next: validateNewPassword(next),
      confirmation: validatePasswordConfirmation(confirmation, next),
    };
    setErrors(nextErrors);
    setFormError(null);
    setSuccess(null);
    if (nextErrors.current) {
      currentRef.current?.focus();
      return;
    }
    if (nextErrors.next) {
      nextRef.current?.focus();
      return;
    }
    if (nextErrors.confirmation) {
      confirmationRef.current?.focus();
      return;
    }

    setPending(true);
    try {
      await changePassword({ current_password: current, new_password: next });
      setCurrent("");
      setNext("");
      setConfirmation("");
      setSuccess("Your password has been changed. Other sessions have been signed out.");
    } catch (caught) {
      setFormError(getErrorMessage(caught));
    } finally {
      setPending(false);
    }
  }

  return (
    <form
      noValidate
      onSubmit={handleSubmit}
      aria-labelledby="password-heading"
      aria-busy={pending}
      className="flex flex-col gap-4"
    >
      <h2 id="password-heading" className="text-xl font-semibold text-fg">
        Change password
      </h2>

      <div role="alert" aria-live="assertive">
        {formError ? (
          <p className="rounded-md border border-danger px-3 py-2 text-sm text-fg">{formError}</p>
        ) : null}
      </div>
      <div role="status">
        {success ? (
          <p className="rounded-md border border-success px-3 py-2 text-sm text-fg">{success}</p>
        ) : null}
      </div>

      <TextField
        ref={currentRef}
        id="password-current"
        name="current_password"
        type="password"
        label="Current password"
        autoComplete="current-password"
        required
        value={current}
        onChange={(event) => setCurrent(event.target.value)}
        error={errors.current}
      />
      <TextField
        ref={nextRef}
        id="password-new"
        name="new_password"
        type="password"
        label="New password"
        autoComplete="new-password"
        hint="8 to 256 characters."
        required
        value={next}
        onChange={(event) => setNext(event.target.value)}
        error={errors.next}
      />
      <TextField
        ref={confirmationRef}
        id="password-confirm"
        name="confirm_password"
        type="password"
        label="Confirm new password"
        autoComplete="new-password"
        required
        value={confirmation}
        onChange={(event) => setConfirmation(event.target.value)}
        error={errors.confirmation}
      />

      <Button type="submit" disabled={pending}>
        {pending ? "Changing password…" : "Change password"}
      </Button>
    </form>
  );
}
