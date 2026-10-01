import { render, screen } from "@testing-library/react";
import { Inbox } from "lucide-react";
import { describe, expect, it } from "vitest";

import { EmptyState } from "./empty-state";
import { ListSkeleton } from "./list-skeleton";
import { PageHeader } from "./page-header";

describe("base components", () => {
  it("EmptyState shows title, description and the next action", () => {
    render(
      <EmptyState
        icon={Inbox}
        title="No messages"
        description="Connect an account."
        action={<button type="button">Open settings</button>}
      />,
    );
    expect(screen.getByRole("heading", { name: "No messages" })).toBeInTheDocument();
    expect(screen.getByText("Connect an account.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open settings" })).toBeInTheDocument();
  });

  it("ListSkeleton renders an accessible loading placeholder", () => {
    render(<ListSkeleton rows={5} />);
    expect(screen.getByRole("status")).toHaveAttribute("aria-busy", "true");
    expect(screen.getAllByTestId("list-skeleton-row")).toHaveLength(5);
  });

  it("PageHeader renders the page title as h1 with actions", () => {
    render(<PageHeader title="Inbox" meta="12" actions={<button type="button">Filter</button>} />);
    expect(screen.getByRole("heading", { level: 1, name: "Inbox" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Filter" })).toBeInTheDocument();
  });
});
