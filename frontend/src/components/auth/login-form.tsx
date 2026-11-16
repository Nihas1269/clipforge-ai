"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useRef, useState, type FormEvent } from "react";

import { Button } from "@/components/button";
import { TextField } from "@/components/text-field";
import { login } from "@/lib/api/auth";
import { getErrorMessage } from "@/lib/api/client";
import { normalizeEmail, validateEmail, validatePassword } from "@/lib/validation/auth";

interface FieldErrors {
  email?: string;
  password?: string;
}

export interface LoginFormProps {
  /** Shows the "account created" confirmation after registration. */
  registered?: boolean;
}

export function LoginForm({ registered = false }: LoginFormProps) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState<FieldErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) {
      return;
    }

    const nextErrors: FieldErrors = {
      email: validateEmail(email),
      password: validatePassword(password),
    };
    setErrors(nextErrors);
    setFormError(null);
    if (nextErrors.email) {
      emailRef.current?.focus();
      return;
    }
    if (nextErrors.password) {
      passwordRef.current?.focus();
      return;
    }

    setPending(true);
    try {
      await login({ email: normalizeEmail(email), password });
    } catch (error) {
      setFormError(getErrorMessage(error));
      setPending(false);
      return;
    }
    // Stay pending while navigating so the form cannot be submitted twice.
    router.replace("/dashboard");
  }

  return (
    <form noValidate onSubmit={handleSubmit} className="flex flex-col gap-4" aria-busy={pending}>
      {registered ? (
        <p
          role="status"
          className="rounded-md border border-success px-3 py-2 text-sm text-fg"
        >
          Account created. You can now log in.
        </p>
      ) : null}

      <div role="alert" aria-live="assertive">
        {formError ? (
          <p className="rounded-md border border-danger px-3 py-2 text-sm text-fg">{formError}</p>
        ) : null}
      </div>

      <TextField
        ref={emailRef}
        id="login-email"
        name="email"
        type="email"
        label="Email"
        autoComplete="email"
        required
        value={email}
        onChange={(event) => setEmail(event.target.value)}
        error={errors.email}
      />
      <TextField
        ref={passwordRef}
        id="login-password"
        name="password"
        type="password"
        label="Password"
        autoComplete="current-password"
        required
        value={password}
        onChange={(event) => setPassword(event.target.value)}
        error={errors.password}
      />

      <Button type="submit" disabled={pending}>
        {pending ? "Logging in…" : "Log in"}
      </Button>

      <p className="text-sm text-fg-muted">
        No account yet?{" "}
        <Link href="/register" className="text-accent underline-offset-2 hover:underline">
          Create one
        </Link>
      </p>
    </form>
  );
}
