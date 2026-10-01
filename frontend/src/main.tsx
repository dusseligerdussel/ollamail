import "@fontsource-variable/inter";
import "./index.css";
import "./i18n";

import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { AppProviders } from "./components/app-providers";
import { createQueryClient } from "./query-client";
import { registerServiceWorker } from "./register-service-worker";
import { createAppRouter } from "./router";

const queryClient = createQueryClient();
const router = createAppRouter(queryClient);

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("Root element #root not found");
}

createRoot(rootElement).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AppProviders>
        <RouterProvider router={router} />
      </AppProviders>
    </QueryClientProvider>
  </StrictMode>,
);

if (import.meta.env.PROD) {
  registerServiceWorker();
}
