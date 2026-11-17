import type { ComponentProps } from "react";

export type ButtonVariant = "primary" | "secondary";

/** Includes `ref`, which React 19 passes to function components as a prop. */
export interface ButtonProps extends ComponentProps<"button"> {
  variant?: ButtonVariant;
}

const baseClasses =
  "inline-flex items-center justify-center gap-2 rounded-md px-4 py-2 text-sm font-medium transition-colors " +
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent " +
  "disabled:cursor-not-allowed disabled:opacity-50";

const variantClasses: Record<ButtonVariant, string> = {
  primary: "bg-accent text-accent-fg hover:bg-accent-hover",
  secondary: "border border-border bg-surface-raised text-fg hover:bg-surface-hover",
};

/** Defaults to `type="button"` so it never submits a form by accident. */
export function Button({ variant = "primary", type = "button", className, ...props }: ButtonProps) {
  return (
    <button
      type={type}
      className={[baseClasses, variantClasses[variant], className].filter(Boolean).join(" ")}
      {...props}
    />
  );
}
