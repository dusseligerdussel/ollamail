import type { QueryClient } from "@tanstack/react-query";
import { createRootRouteWithContext, redirect, useMatches } from "@tanstack/react-router";

import { currentUserQueryOptions, safeRedirect, setupStatusQueryOptions } from "@/api/auth";
import { AppShell } from "@/components/app-shell/app-shell";
import { NotFound } from "@/components/not-found";
import { PublicLayout } from "@/components/public-layout";
import { RootError } from "@/components/root-error";

export interface RouterContext {
  queryClient: QueryClient;
}

declare module "@tanstack/react-router" {
  interface StaticDataRouteOption {
    /** Reachable without a session and rendered without the app shell (login, setup). */
    public?: boolean;
  }
}

export const Route = createRootRouteWithContext<RouterContext>()({
  // Closed by default: every route needs a session unless it is handled here explicitly.
  // The API enforces permissions; the guard only decides what to show.
  beforeLoad: async ({ context: { queryClient }, location }) => {
    const path = location.pathname;
    const { initialized } = await queryClient.ensureQueryData(setupStatusQueryOptions);
    if (!initialized) {
      if (path !== "/setup") throw redirect({ to: "/setup", replace: true });
      return;
    }
    const user = await queryClient.ensureQueryData(currentUserQueryOptions);
    if (path === "/setup") {
      throw redirect({ to: user ? "/inbox" : "/login", replace: true });
    }
    if (path === "/login") {
      const target = (location.search as { redirect?: unknown }).redirect;
      if (user) throw redirect({ href: safeRedirect(target), replace: true });
      return;
    }
    if (!user) {
      throw redirect({ to: "/login", search: { redirect: location.href }, replace: true });
    }
  },
  component: RootLayout,
  notFoundComponent: NotFound,
  errorComponent: RootError,
});

function RootLayout() {
  const isPublic = useMatches({ select: (matches) => matches.some((m) => m.staticData.public) });
  return isPublic ? <PublicLayout /> : <AppShell />;
}
