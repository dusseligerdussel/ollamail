import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import i18n from "./i18n";
import { createQueryClient } from "./query-client";
import { createAppRouter } from "./router";

function renderApp(path = "/") {
  const queryClient = createQueryClient();
  const router = createAppRouter(queryClient, createMemoryHistory({ initialEntries: [path] }));
  return render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

describe("placeholder page", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
  });

  it("renders the translated placeholder", async () => {
    renderApp();
    expect(await screen.findByRole("heading", { name: "ollamail" })).toBeInTheDocument();
    expect(screen.getByText(/interface is under construction/i)).toBeInTheDocument();
  });

  it("switches the language", async () => {
    renderApp();
    await userEvent.click(await screen.findByRole("button", { name: "Deutsch" }));
    expect(await screen.findByText(/Oberfläche befindet sich im Aufbau/)).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("de");
  });
});
