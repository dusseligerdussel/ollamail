interface RetryableQuery {
  isError: boolean;
  isFetching: boolean;
  refetch: () => Promise<unknown>;
}

/**
 * `onRetry` and `retrying` for an `InlineError` in a view built from several queries: loads the
 * failed ones again.
 */
export function retryFailed(...queries: RetryableQuery[]) {
  return {
    onRetry: () =>
      Promise.all(queries.filter((query) => query.isError).map((query) => query.refetch())),
    retrying: queries.some((query) => query.isFetching),
  };
}
