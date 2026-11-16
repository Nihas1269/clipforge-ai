import type { ComponentProps } from "react";

export interface TextFieldProps extends Omit<ComponentProps<"input">, "id"> {
  id: string;
  label: string;
  /** Shown below the input; also marks the input as invalid. */
  error?: string;
  hint?: string;
}

/**
 * Labeled input. The error text sits in a polite live region linked to the
 * input with `aria-describedby`, so screen readers announce and re-read it.
 */
export function TextField({ id, label, error, hint, className, ...props }: TextFieldProps) {
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const describedBy = [hint ? hintId : null, error ? errorId : null].filter(Boolean).join(" ");

  return (
    <div className={["flex flex-col gap-1.5", className].filter(Boolean).join(" ")}>
      <label htmlFor={id} className="text-sm font-medium text-fg">
        {label}
      </label>
      {hint ? (
        <p id={hintId} className="text-xs text-fg-muted">
          {hint}
        </p>
      ) : null}
      <input
        id={id}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy || undefined}
        className={
          "rounded-md border bg-surface-raised px-3 py-2 text-sm text-fg " +
          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent " +
          (error ? "border-danger" : "border-border")
        }
        {...props}
      />
      <p id={errorId} aria-live="polite" className="min-h-5 text-sm text-danger">
        {error}
      </p>
    </div>
  );
}
