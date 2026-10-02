import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { backend, mockFetch, problem } from "@/test/fetch";
import { renderApp } from "@/test/render-app";

describe("settings: server status (example query on /healthz)", () => {
  it("shows the connection as connected", async () => {
    await renderApp("/settings");

    expect(await screen.findByText("Connected")).toBeInTheDocument();
  });

  it("shows API errors inline with the request ID", async () => {
    const api = backend();
    mockFetch((request) =>
      new URL(request.url).pathname === "/api/healthz"
        ? problem(503, { request_id: "req-42" })
        : api(request),
    );
    await renderApp("/settings");

    const alert = await screen.findByRole("alert", {}, { timeout: 5000 });
    expect(alert).toHaveTextContent("The service is temporarily unavailable.");
    expect(alert).toHaveTextContent("Request ID: req-42");
  });
});
