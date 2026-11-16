import type { ReactNode } from "react";

export type StatusVariant = "loading" | "empty" | "error" | "success";

export interface StatusMessageProps {
  variant: StatusVariant;
  title: string;
  description?: string;
  /** Optional follow-up control, such as a retry button. */
  action?: ReactNode;
}

const indicatorClasses: Record<StatusVariant, string> = {
  loading: "border-2 border-border border-t-accent motion-safe:animate-spin",
  empty: "border-2 border-dashed border-fg-muted",
  error: "bg-danger",
  success: "bg-success",
};

/**
 * Shared presentation for loading, empty, error, and success states.
 * Errors use `role="alert"` so assistive technology announces them
 * immediately; the other states use the polite `role="status"`.
 */
export function StatusMessage({ variant, title, description, action }: StatusMessageProps) {
  return (
    <div
      role={variant === "error" ? "alert" : "status"}
      data-variant={variant}
      className="flex flex-col items-center gap-3 rounded-lg border border-border bg-surface px-6 py-10 text-center"
    >
      <span aria-hidden="true" className={`size-6 rounded-full ${indicatorClasses[variant]}`} />
      <p className="text-base font-semibold text-fg">{title}</p>
      {description ? <p className="max-w-md text-sm text-fg-muted">{description}</p> : null}
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  );
}
