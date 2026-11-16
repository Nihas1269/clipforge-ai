import type { HTMLAttributes } from "react";

/** Centers page content and applies the responsive horizontal gutter. */
export function Container({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={["mx-auto w-full max-w-6xl px-4 sm:px-6 lg:px-8", className]
        .filter(Boolean)
        .join(" ")}
      {...props}
    />
  );
}
