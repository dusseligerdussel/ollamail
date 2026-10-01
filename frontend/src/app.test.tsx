import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Archive } from "lucide-react";
import { useMemo } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useCommands } from "@/components/command-palette/command-provider";
import type { CurrentUser } from "@/hooks/use-current-user";
import type { Command } from "@/lib/commands";

import i18n from "./i18n";
import { setCoarsePointer, setViewportWidth } from "./test/media";
import { renderApp } from "./test/render-app";

const currentUser = vi.hoisted(() => ({ value: { id: "test", isAdmin: true } as CurrentUser }));
vi.mock("@/hooks/use-current-user", () => ({ useCurrentUser: () => currentUser.value }));

beforeEach(async () => {
  currentUser.value = { id: "test", isAdmin: true };
  await i18n.changeLanguage("en");
});

describe("app shell", () => {
  it("redirects to the inbox and renders the navigation", async () => {
    const { router } = await renderApp("/");
    expect(router.state.location.pathname).toBe("/inbox");
    expect(screen.getByRole("heading", { level: 1, name: "Inbox" })).toBeInTheDocument();

    const nav = screen.getByRole("navigation", { name: "Main navigation" });
    const links = within(nav)
      .getAllByRole("link")
      .map((link) => link.textContent);
    expect(links).toEqual(["Inbox", "Tasks", "Digest", "Search", "Settings", "Admin"]);
    expect(within(nav).getByRole("link", { name: "Inbox" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("hides the admin area from non-admins", async () => {
    currentUser.value = { id: "test", isAdmin: false };
    await renderApp("/admin");
    expect(screen.getByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Main navigation" });
    expect(within(nav).queryByRole("link", { name: "Admin" })).not.toBeInTheDocument();
  });

  it("navigates with g-sequences", async () => {
    const user = userEvent.setup();
    const { router } = await renderApp();
    await user.keyboard("gt");
    await screen.findByRole("heading", { level: 1, name: "Tasks" });
    await user.keyboard("/");
    await screen.findByRole("heading", { level: 1, name: "Search" });
    expect(router.state.location.pathname).toBe("/search");
  });

  it("shows the shortcut overview with ?", async () => {
    const user = userEvent.setup();
    await renderApp();
    await user.keyboard("?");
    const sheet = await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    expect(within(sheet).getByText("Next item")).toBeInTheDocument();
    expect(within(sheet).getByText("Go to Tasks")).toBeInTheDocument();
    // Initial focus is on the list, not on the close button (no focus ring right away).
    await waitFor(() => expect(sheet).toContainElement(document.activeElement as HTMLElement));
    expect(within(sheet).getByRole("button", { name: "Close" })).not.toHaveFocus();
  });

  it("hides keyboard hints in the sidebar on touch devices", async () => {
    setCoarsePointer(true);
    await renderApp();
    expect(screen.getByRole("button", { name: "Command menu" })).toHaveTextContent(
      /^Command menu$/,
    );
    expect(screen.queryByRole("button", { name: /Keyboard shortcuts/ })).not.toBeInTheDocument();
  });

  it("links from the empty admin area to the settings", async () => {
    const user = userEvent.setup();
    await renderApp("/admin");
    await user.click(screen.getByRole("link", { name: "Open your settings" }));
    await screen.findByRole("heading", { level: 1, name: "Settings" });
  });

  it("uses a bottom bar and a sheet on narrow screens", async () => {
    setViewportWidth(375);
    const user = userEvent.setup();
    await renderApp();
    const nav = screen.getByRole("navigation", { name: "Main navigation" });
    expect(within(nav).queryByRole("link", { name: "Settings" })).not.toBeInTheDocument();

    await user.click(within(nav).getByRole("button", { name: "More" }));
    const sheet = await screen.findByRole("dialog", { name: "More" });
    await user.click(within(sheet).getByRole("link", { name: "Settings" }));
    await screen.findByRole("heading", { level: 1, name: "Settings" });
  });

  it("switches the language", async () => {
    const user = userEvent.setup();
    await renderApp("/settings");
    await user.click(screen.getByRole("radio", { name: "Deutsch" }));
    expect(
      await screen.findByRole("heading", { level: 1, name: "Einstellungen" }),
    ).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("de");
  });

  it("switches the theme from the settings page", async () => {
    const user = userEvent.setup();
    await renderApp("/settings");
    await user.click(screen.getByRole("radio", { name: "Dark" }));
    expect(document.documentElement).toHaveClass("dark");
  });
});

describe("command palette", () => {
  it("opens with Ctrl+K and runs a navigation command", async () => {
    const user = userEvent.setup();
    const { router } = await renderApp();

    await user.keyboard("{Control>}k{/Control}");
    const dialog = await screen.findByRole("dialog", { name: "Command menu" });
    const input = within(dialog).getByRole("combobox");
    expect(input).toHaveFocus();

    await user.type(input, "tasks");
    expect(
      within(dialog)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual([expect.stringContaining("Go to Tasks")]);
    await user.keyboard("{Enter}");

    await screen.findByRole("heading", { level: 1, name: "Tasks" });
    expect(router.state.location.pathname).toBe("/tasks");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("runs appearance commands", async () => {
    const user = userEvent.setup();
    await renderApp();
    await user.keyboard("{Control>}k{/Control}");
    await user.type(await screen.findByRole("combobox"), "dark theme");
    await user.keyboard("{Enter}");
    expect(document.documentElement).toHaveClass("dark");
  });

  it("lists commands registered by pages", async () => {
    const run = vi.fn();
    function ArchiveCommand() {
      const commands = useMemo<Command[]>(
        () => [{ id: "test.archive", label: "Archive all", group: "actions", icon: Archive, run }],
        [],
      );
      useCommands(commands);
      return null;
    }

    const user = userEvent.setup();
    await renderApp("/inbox", { extra: <ArchiveCommand /> });
    await user.keyboard("{Control>}k{/Control}");
    const dialog = await screen.findByRole("dialog", { name: "Command menu" });
    expect(within(dialog).getByRole("group", { name: "Actions" })).toBeInTheDocument();

    await user.click(within(dialog).getByRole("option", { name: /Archive all/ }));
    expect(run).toHaveBeenCalledOnce();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("shows shortcut hints and the shortcut overview only where a keyboard is likely", async () => {
    const user = userEvent.setup();
    await renderApp();
    await user.keyboard("{Control>}k{/Control}");
    let dialog = await screen.findByRole("dialog", { name: "Command menu" });
    expect(within(dialog).getByRole("option", { name: /Go to Tasks/ })).toHaveTextContent("then");
    expect(
      within(dialog).getByRole("option", { name: /Show keyboard shortcuts/ }),
    ).toBeInTheDocument();
    await act(() => user.keyboard("{Escape}"));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    act(() => setViewportWidth(390));
    await user.keyboard("{Control>}k{/Control}");
    dialog = await screen.findByRole("dialog", { name: "Command menu" });
    expect(within(dialog).getByRole("option", { name: /Go to Tasks/ })).not.toHaveTextContent(
      "then",
    );
    expect(
      within(dialog).queryByRole("option", { name: /Show keyboard shortcuts/ }),
    ).not.toBeInTheDocument();
  });

  it("marks the list edge when more commands are hidden below or above", async () => {
    const isList = (element: HTMLElement) => element.dataset.slot === "command-list";
    const scrollHeight = vi
      .spyOn(HTMLElement.prototype, "scrollHeight", "get")
      .mockImplementation(function (this: HTMLElement) {
        return isList(this) ? 1000 : 0;
      });
    const clientHeight = vi
      .spyOn(HTMLElement.prototype, "clientHeight", "get")
      .mockImplementation(function (this: HTMLElement) {
        return isList(this) ? 360 : 0;
      });
    try {
      const user = userEvent.setup();
      await renderApp();
      await user.keyboard("{Control>}k{/Control}");
      const dialog = await screen.findByRole("dialog", { name: "Command menu" });
      const list = within(dialog).getByRole("listbox");
      await waitFor(() => expect(list).toHaveAttribute("data-overflow", "bottom"));

      list.scrollTop = 300;
      fireEvent.scroll(list);
      expect(list).toHaveAttribute("data-overflow", "both");

      list.scrollTop = 640;
      fireEvent.scroll(list);
      expect(list).toHaveAttribute("data-overflow", "top");
    } finally {
      scrollHeight.mockRestore();
      clientHeight.mockRestore();
    }
  });

  it("toggles closed with Ctrl+K from inside the input", async () => {
    const user = userEvent.setup();
    await renderApp();
    await user.keyboard("{Control>}k{/Control}");
    await screen.findByRole("dialog", { name: "Command menu" });
    await act(() => user.keyboard("{Control>}k{/Control}"));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
});
