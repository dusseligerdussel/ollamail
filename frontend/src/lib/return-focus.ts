// Overlays whose focus is temporary: focus inside them is never a place to return to.
const overlaySelector =
  '[role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"], [data-radix-popper-content-wrapper]';

let lastOutside: HTMLElement | null = null;

if (typeof document !== "undefined") {
  document.addEventListener(
    "focusin",
    (event) => {
      const target = event.target;
      if (
        target instanceof HTMLElement &&
        target !== document.body &&
        !target.closest(overlaySelector)
      )
        lastOutside = target;
    },
    true,
  );
}

/**
 * `onCloseAutoFocus` of dialogs and sheets: returns the focus to where it was before the overlay
 * opened (WCAG 2.4.3). Radix only knows a `Trigger`; overlays opened by a shortcut, the command
 * palette or a menu item would otherwise leave the focus on `<body>`.
 */
export function returnFocus(event: Event) {
  event.preventDefault();
  const current = document.activeElement;
  // An action of the overlay may have moved the focus elsewhere on purpose.
  if (
    current instanceof HTMLElement &&
    current !== document.body &&
    !current.closest(overlaySelector)
  )
    return;
  if (lastOutside?.isConnected) lastOutside.focus({ preventScroll: true });
}
