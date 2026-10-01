import { queryOptions } from "@tanstack/react-query";

import { api, unwrap } from "./client";

/** Example query: liveness of the API (`GET /healthz`). */
export const healthQueryOptions = queryOptions({
  queryKey: ["health"],
  queryFn: ({ signal }) => unwrap(api.GET("/healthz", { signal })),
  // Errors are shown inline next to the status, not as a toast.
  meta: { errorToast: false },
});
