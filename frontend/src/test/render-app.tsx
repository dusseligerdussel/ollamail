import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";

import { AppProviders } from "@/components/app-providers";
import { createQueryClient } from "@/query-client";
import { createAppRouter } from "@/router";

/** Renders the full app (providers, shell, routes) at the given path. */
export async function renderApp(path = "/inbox", { extra }: { extra?: ReactNode } = {}) {
  const queryClient = createQueryClient();
  const router = createAppRouter(queryClient, createMemoryHistory({ initialEntries: [path] }));
  const result = render(
    <QueryClientProvider client={queryClient}>
      <AppProviders>
        <RouterProvider router={router} />
        {extra}
      </AppProviders>
    </QueryClientProvider>,
  );
  await screen.findByRole("heading", { level: 1 });
  return { ...result, router };
}
