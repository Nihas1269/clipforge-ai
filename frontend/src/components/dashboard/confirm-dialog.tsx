"use client";

import { useEffect, useId, useRef, type KeyboardEvent, type ReactNode } from "react";

import { Button } from "@/components/button";

export interface ConfirmDialogProps {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  busyLabel?: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * A modal confirmation (`role="alertdialog"`). Focus moves to Cancel, the safe
 * choice, when it opens; Tab stays inside the dialog; Escape cancels; focus
 * returns to the element that opened it when it closes (if it still exists).
 */
export function ConfirmDialog({
  title,
  children,
  confirmLabel,
  busyLabel,
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const titleId = useId();
  const descriptionId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    cancelRef.current?.focus();
    return () => {
      if (opener && opener.isConnected) {
        opener.focus();
      }
    };
  }, []);

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      if (!busy) {
        onCancel();
      }
      return;
    }
    if (event.key !== "Tab" || !dialogRef.current) {
      return;
    }
    const focusable = Array.from(
      dialogRef.current.querySelectorAll<HTMLElement>("button:not([disabled])"),
    );
    if (focusable.length === 0) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-canvas/80 px-4">
      <div
        ref={dialogRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        aria-busy={busy}
        onKeyDown={handleKeyDown}
        className="w-full max-w-md rounded-lg border border-border bg-surface-raised p-6 shadow-lg"
      >
        <h2 id={titleId} className="text-lg font-semibold text-fg">
          {title}
        </h2>
        <div id={descriptionId} className="mt-2 text-sm text-fg-muted">
          {children}
        </div>
        <div className="mt-6 flex justify-end gap-3">
          <Button ref={cancelRef} variant="secondary" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={onConfirm} disabled={busy}>
            {busy && busyLabel ? busyLabel : confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}
