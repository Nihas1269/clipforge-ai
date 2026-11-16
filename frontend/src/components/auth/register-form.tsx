"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useRef, useState, type FormEvent } from "react";

import { Button } from "@/components/button";
import { TextField } from "@/components/text-field";
import { register } from "@/lib/api/auth";
import { getErrorMessage } from "@/lib/api/client";
import {
  PASSWORD_MIN_LENGTH,
  normalizeEmail,
  validateEmail,
  validatePassword,
} from "@/lib/validation/auth";

interface FieldErrors {
  email?: string;
  password?: string;
}

export function RegisterForm() {
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
      await register({ email: normalizeEmail(email), password });
    } catch (error) {
      setFormError(getErrorMessage(error));
      setPending(false);
      return;
    }
    // Stay pending while navigating so the form cannot be submitted twice.
    router.push("/login?registered=1");
  }

  return (
    <form noValidate onSubmit={handleSubmit} className="flex flex-col gap-4" aria-busy={pending}>
      <div role="alert" aria-live="assertive">
        {formError ? (
          <p className="rounded-md border border-danger px-3 py-2 text-sm text-fg">{formError}</p>
        ) : null}
      </div>

      <TextField
        ref={emailRef}
        id="register-email"
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
        id="register-password"
        name="password"
        type="password"
        label="Password"
        hint={`At least ${PASSWORD_MIN_LENGTH} characters.`}
        autoComplete="new-password"
        required
        value={password}
        onChange={(event) => setPassword(event.target.value)}
        error={errors.password}
      />

      <Button type="submit" disabled={pending}>
        {pending ? "Creating account…" : "Create account"}
      </Button>

      <p className="text-sm text-fg-muted">
        Already have an account?{" "}
        <Link href="/login" className="text-accent underline-offset-2 hover:underline">
          Log in
        </Link>
      </p>
    </form>
  );
}
