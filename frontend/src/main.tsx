import "@fontsource-variable/inter";
import "./index.css";

import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { AppProviders } from "./components/app-providers";
import { i18nReady } from "./i18n";
import { createQueryClient } from "./query-client";
import { registerServiceWorker } from "./register-service-worker";
import { createAppRouter } from "./router";

const queryClient = createQueryClient();
const router = createAppRouter(queryClient);

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("Root element #root not found");
}

// The texts of the active language are a chunk of their own: render once they are loaded.
void i18nReady.finally(() => {
  createRoot(rootElement).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <AppProviders>
          <RouterProvider router={router} />
        </AppProviders>
      </QueryClientProvider>
    </StrictMode>,
  );
});

if (import.meta.env.PROD) {
  registerServiceWorker();
}
