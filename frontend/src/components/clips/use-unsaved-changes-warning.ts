"use client";

import { useEffect } from "react";

export const UNSAVED_CHANGES_MESSAGE = "You have unsaved changes. Leave this page and discard them?";

/** Whether a click on `anchor` would leave the current page. */
function leavesPage(anchor: HTMLAnchorElement): boolean {
  if ((anchor.target && anchor.target !== "_self") || anchor.hasAttribute("download")) {
    return false;
  }
  const url = new URL(anchor.href, window.location.href);
  const here = new URL(window.location.href);
  // A link to an anchor on this page does not navigate away.
  return url.origin !== here.origin || url.pathname !== here.pathname || url.search !== here.search;
}

/**
 * While `active`, asks before the page is left: the browser's own prompt on
 * reload, close and address-bar navigation (`beforeunload`), and a
 * confirmation before following a link (including Next.js `<Link>`s, whose
 * client-side navigation is cancelled when the user declines). Nothing is
 * registered while there are no unsaved changes.
 */
export function useUnsavedChangesWarning(active: boolean, message: string = UNSAVED_CHANGES_MESSAGE): void {
  useEffect(() => {
    if (!active) {
      return;
    }
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      // Older browsers show the prompt only when returnValue is set.
      event.returnValue = "";
    };
    // Capture phase on the document runs before React's (and so Next.js Link's) handlers.
    const handleClick = (event: MouseEvent) => {
      if (
        event.defaultPrevented ||
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey
      ) {
        return;
      }
      const anchor = event.target instanceof Element ? event.target.closest("a[href]") : null;
      if (!(anchor instanceof HTMLAnchorElement) || !leavesPage(anchor)) {
        return;
      }
      if (!window.confirm(message)) {
        event.preventDefault();
        event.stopPropagation();
      }
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    document.addEventListener("click", handleClick, true);
    return () => {
      window.removeEventListener("beforeunload", handleBeforeUnload);
      document.removeEventListener("click", handleClick, true);
    };
  }, [active, message]);
}
