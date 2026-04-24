/** Small pure helpers for the `#`-triggered Drive file mention picker.
 *  Kept separate from chat-input.tsx so the trigger logic is unit-testable
 *  without rendering the whole input component.
 */

const DRIVE_TRIGGER_RE = /(?:^|\s)#([^\s]*)$/;

/**
 * Return the query string the user has typed after the trailing `#`, or
 * `null` when `value` doesn't end in a `#`-mention. The `#` must be at
 * start-of-string or preceded by whitespace — a hashtag inside a word
 * (`foo#bar`) deliberately does NOT trigger the picker.
 */
export function detectDriveTrigger(value: string): string | null {
  const match = value.match(DRIVE_TRIGGER_RE);
  if (!match) return null;
  return match[1];
}

/**
 * Replace the trailing `#<query>` with a markdown link + trailing space.
 * Callers typically pass `insertDriveMention(value, "[Name](url)")`; the
 * helper adds the space so the user can keep typing without a separator.
 */
export function insertDriveMention(value: string, insertion: string): string {
  return value.replace(/#[^\s]*$/, `${insertion} `);
}
