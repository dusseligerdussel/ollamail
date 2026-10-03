import type { TFunction } from "i18next";

/** RFC 9457 Problem Details as returned by the backend (`app/core/errors.py`). */
export interface ProblemDetails {
  type?: string;
  title?: string;
  status?: number;
  detail?: string;
  instance?: string;
  request_id?: string;
  /** Validation errors (422): location and message only, never the rejected input. */
  errors?: { loc: (string | number)[]; msg: string; type: string }[];
  [extension: string]: unknown;
}

/**
 * Failed API request. `status` is 0 if the server could not be reached.
 *
 * `problem.detail` is English server text: show it only as secondary information, the
 * user-facing message comes from `describeApiError`.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly problem: ProblemDetails | undefined;

  constructor(status: number, problem?: ProblemDetails, options?: ErrorOptions) {
    super(problem?.title ?? (status === 0 ? "Network error" : `HTTP ${status}`), options);
    this.name = "ApiError";
    this.status = status;
    this.problem = problem;
  }

  get requestId(): string | undefined {
    return this.problem?.request_id;
  }

  get isUnauthorized(): boolean {
    return this.status === 401;
  }

  /** 4xx: retrying the same request will not help. */
  get isClientError(): boolean {
    return this.status >= 400 && this.status < 500;
  }
}

export function isProblemDetails(value: unknown): value is ProblemDetails {
  if (typeof value !== "object" || value === null) return false;
  const { title, status } = value as Record<string, unknown>;
  return typeof title === "string" || typeof status === "number";
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

/** `error_code` of the backend's CSRF rejection (`app/auth/csrf.py`). */
export const CSRF_ERROR_CODE = "csrf_failed";

/**
 * The server rejected a state-changing request because the CSRF cookie was missing or
 * invalid, typically because the browser dropped the `Secure` cookie on plain `http://`.
 */
export function isCsrfError(error: unknown): boolean {
  return isApiError(error) && error.status === 403 && error.problem?.error_code === CSRF_ERROR_CODE;
}

const messageKeys = {
  0: "errors.network",
  400: "errors.badRequest",
  401: "errors.unauthorized",
  403: "errors.forbidden",
  404: "errors.notFound",
  409: "errors.conflict",
  413: "errors.tooLarge",
  422: "errors.validation",
  429: "errors.rateLimited",
  503: "errors.unavailable",
} as const;

function messageKey(error: unknown) {
  if (!isApiError(error)) return "errors.unknown" as const;
  const known = messageKeys[error.status as keyof typeof messageKeys];
  if (known) return known;
  return error.status >= 500 ? ("errors.server" as const) : ("errors.unknown" as const);
}

export interface ErrorDescription {
  /** Localized, user-facing message. */
  title: string;
  /** Request ID for support, if the server sent one. */
  description?: string;
}

/** User-facing text for any error thrown by an API call (toast or inline). */
export function describeApiError(error: unknown, t: TFunction): ErrorDescription {
  const title = t(messageKey(error));
  const requestId = isApiError(error) ? error.requestId : undefined;
  return requestId ? { title, description: t("errors.requestId", { id: requestId }) } : { title };
}
