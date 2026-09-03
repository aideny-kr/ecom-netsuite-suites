/**
 * One flag, one writer, one reader — "is the ⌘K palette on screen right now?"
 *
 * The ⌘K palette (`celigo-command-palette.tsx`) and the flow page
 * (`celigo-flow-page.tsx`) both answer Escape, and they are siblings under
 * `CeligoSurface` with no shared React state between them. Radix closes the
 * palette from a document-level CAPTURE listener and does NOT stop the event,
 * so the flow page's own window listener saw the same Escape and cleared the
 * selected step behind the palette: one keypress, two dismissals (final-review
 * finding I5).
 *
 * The flag lives on `document.body` rather than in a module variable so it is
 * observable from a test and from the DOM inspector, and so a stale module
 * instance can never disagree with what is actually mounted. It is set from
 * the palette's own effect and cleared on unmount, which means it survives
 * exactly as long as the dialog does.
 *
 * Ordering this depends on: Radix's document capture listener runs before the
 * page's window bubble listener, and React has not yet flushed the close-state
 * render (nor the effect that clears this attribute) by the time the bubble
 * listener runs. So during the Escape that CLOSES the palette, this still
 * reads true — which is the whole point.
 */

const PALETTE_OPEN_ATTR = "data-celigo-palette-open";

export function setCeligoPaletteOpen(open: boolean): void {
  if (typeof document === "undefined") return;
  if (open) document.body.setAttribute(PALETTE_OPEN_ATTR, "true");
  else document.body.removeAttribute(PALETTE_OPEN_ATTR);
}

export function isCeligoPaletteOpen(): boolean {
  if (typeof document === "undefined") return false;
  return document.body.hasAttribute(PALETTE_OPEN_ATTR);
}
