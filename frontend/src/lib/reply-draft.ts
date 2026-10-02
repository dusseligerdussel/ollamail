import { isApiError } from "@/api/errors";

/**
 * Text of the last suggestion per draft, kept in memory for this page load only (never in
 * storage: it is mail content). Sending a draft whose text still equals it asks for an extra
 * confirmation.
 */
const suggestions = new Map<string, string>();

export function rememberSuggestion(draftId: string, text: string) {
  suggestions.set(draftId, text);
}

export function suggestionOf(draftId: string): string | undefined {
  return suggestions.get(draftId);
}

export function forgetSuggestion(draftId: string) {
  suggestions.delete(draftId);
}

/** The text is still the suggestion as generated (whitespace at the ends does not count). */
export function isUnchangedSuggestion(text: string, suggestion: string | undefined): boolean {
  return suggestion !== undefined && text.trim() === suggestion.trim();
}

/** Error codes of the generation stream, plus `interrupted` for a stream that just ends. */
export const generateErrors = [
  "llm_unavailable",
  "llm_cloud_disabled",
  "llm_error",
  "internal",
  "interrupted",
] as const;
export type GenerateError = (typeof generateErrors)[number];

export function generateErrorCode(error: unknown): GenerateError | undefined {
  if (!error || typeof error !== "object" || !("code" in error)) return undefined;
  return generateErrors.find((code) => code === error.code) ?? "internal";
}

/** `error_code`s of `POST /drafts/{id}/send` with their own message. */
const sendErrors = [
  "read_only",
  "message_deleted",
  "draft_closed",
  "no_recipients",
  "invalid_header",
  "recipients_refused",
  "send_not_permitted",
  "message_refused",
  "authentication_failed",
] as const;
export type SendError = (typeof sendErrors)[number] | "rejected" | "unreachable" | "not_configured";

/**
 * Message key (`drafts.sendErrors.<key>`) for a failed send; `undefined` for errors that
 * are not specific to sending (network, 401, …), which use the general error text.
 */
export function sendErrorKey(error: unknown): SendError | undefined {
  if (!isApiError(error)) return undefined;
  const code = error.problem?.error_code;
  const known = sendErrors.find((item) => item === code);
  if (known) return known;
  if (error.status === 502) return "rejected";
  if (error.status === 503) return "unreachable";
  if (error.status === 409 && typeof code === "string") return "not_configured";
  return undefined;
}
