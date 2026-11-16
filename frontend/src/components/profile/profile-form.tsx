"use client";

import { useRef, useState, type FormEvent } from "react";

import { Button } from "@/components/button";
import { useCurrentUser } from "@/components/session/session-provider";
import { TextField } from "@/components/text-field";
import { getErrorMessage } from "@/lib/api/client";
import { updateProfile } from "@/lib/api/users";
import { normalizeFullName, validateFullName } from "@/lib/validation/profile";

/** Edits `full_name`. Rendered only inside the authenticated layout. */
export function ProfileForm() {
  const { user, setUser } = useCurrentUser();
  const [fullName, setFullName] = useState(user?.full_name ?? "");
  const [fieldError, setFieldError] = useState<string | undefined>();
  const [formError, setFormError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) {
      return;
    }

    const error = validateFullName(fullName);
    setFieldError(error);
    setFormError(null);
    setSuccess(null);
    if (error) {
      inputRef.current?.focus();
      return;
    }

    setPending(true);
    try {
      const updated = await updateProfile({ full_name: normalizeFullName(fullName) });
      setUser(updated);
      setFullName(updated.full_name ?? "");
      setSuccess("Your profile has been updated.");
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
      aria-labelledby="profile-heading"
      aria-busy={pending}
      className="flex flex-col gap-4"
    >
      <h2 id="profile-heading" className="text-xl font-semibold text-fg">
        Personal details
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
        id="profile-email"
        name="email"
        type="email"
        label="Email"
        value={user?.email ?? ""}
        readOnly
        hint="Your email address cannot be changed here."
      />
      <TextField
        ref={inputRef}
        id="profile-full-name"
        name="full_name"
        label="Full name"
        autoComplete="name"
        value={fullName}
        onChange={(event) => setFullName(event.target.value)}
        error={fieldError}
      />

      <Button type="submit" disabled={pending}>
        {pending ? "Saving…" : "Save profile"}
      </Button>
    </form>
  );
}
