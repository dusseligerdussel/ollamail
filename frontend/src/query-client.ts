import { MutationCache, QueryCache, QueryClient } from "@tanstack/react-query";
import i18n from "i18next";
import { toast } from "sonner";

import { describeApiError, isApiError } from "@/api/errors";
import { isReauthCancelled } from "@/api/reauth";

declare module "@tanstack/react-query" {
  interface Register {
    queryMeta: ErrorHandlingMeta;
    mutationMeta: ErrorHandlingMeta;
  }
}

export interface ErrorHandlingMeta extends Record<string, unknown> {
  /**
   * Show failures as a toast. Defaults: mutations always; queries only when data is already
   * shown (background refresh failed), otherwise the component renders the error inline.
   * Set `false` when the component handles the error itself.
   */
  errorToast?: boolean;
}

/** Central error toast for API failures; 401 is handled by the API client (login redirect). */
export function showErrorToast(error: unknown) {
  if (isApiError(error) && error.isUnauthorized) return;
  // The user closed the confirmation sheet (components/auth/reauth.tsx): nothing failed.
  if (isReauthCancelled(error)) return;
  const { title, description } = describeApiError(error, i18n.t);
  // One toast per message: repeated failures replace instead of stacking.
  toast.error(title, { id: title, description });
}

const MAX_RETRIES = 2;

function shouldRetry(failureCount: number, error: unknown) {
  if (isApiError(error) && error.isClientError) return false;
  return failureCount < MAX_RETRIES;
}

export function createQueryClient() {
  return new QueryClient({
    queryCache: new QueryCache({
      onError(error, query) {
        const errorToast = query.meta?.errorToast ?? query.state.data !== undefined;
        if (errorToast) showErrorToast(error);
      },
    }),
    mutationCache: new MutationCache({
      onError(error, _variables, _context, mutation) {
        if (mutation.meta?.errorToast !== false) showErrorToast(error);
      },
    }),
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        refetchOnWindowFocus: false,
        retry: shouldRetry,
      },
      mutations: {
        retry: false,
      },
    },
  });
}
