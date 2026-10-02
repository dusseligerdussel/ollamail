import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Mailbox, MailboxProvider } from "@/api/mail";
import { backend, json, mockFetch, problem } from "@/test/fetch";
import { MAILBOX_ID, testMailbox } from "@/test/mail";
import { renderApp } from "@/test/render-app";

interface Recorded {
  method: string;
  path: string;
  body: unknown;
}

function mockMailboxApi({
  mailboxes = [testMailbox()],
  providers = [{ type: "imap", connect: "credentials", oauth_start_path: null }],
  test = { ok: true, error: null, folders: [] },
  create,
  discovered,
}: {
  mailboxes?: Mailbox[];
  providers?: MailboxProvider[];
  test?: unknown;
  create?: Response;
  /** Holds back the autodiscovery answer until it resolves. */
  discovered?: Promise<void>;
} = {}) {
  const calls: Recorded[] = [];
  const base = backend();
  mockFetch(async (request) => {
    const { pathname } = new URL(request.url);
    const route = `${request.method} ${pathname}`;
    const body =
      request.method === "GET"
        ? undefined
        : await request.text().then((text) => (text ? JSON.parse(text) : undefined));
    calls.push({ method: request.method, path: pathname, body });
    switch (route) {
      case "GET /api/mailboxes":
        return json(mailboxes);
      case "GET /api/mailboxes/providers":
        return json(providers);
      case "POST /api/mailboxes/autodiscover":
        await discovered;
        return json({
          suggestions: [
            {
              type: "imap",
              provider_settings: { host: "imap.firma.example", port: 993, security: "tls" },
              source: "guess",
              hints: ["guessed"],
            },
          ],
        });
      case "POST /api/mailboxes/test":
        return json(test);
      case "POST /api/mailboxes":
        return create ?? json(testMailbox({ display_name: "Firma" }), { status: 201 });
      case `DELETE /api/mailboxes/${MAILBOX_ID}`:
        return json({ mailbox_id: MAILBOX_ID, deleted: true, messages: 3, attachments: 0 });
      case `PATCH /api/mailboxes/${MAILBOX_ID}`:
        return json(testMailbox({ sync_enabled: false }));
      case "POST /api/mail/gmail/oauth/start":
        return json({ authorization_url: "https://accounts.example/authorize" });
      default:
        return base(request);
    }
  });
  return calls;
}

/**
 * Types into a field. Focus is set directly: in jsdom (no layout) the shell's resize handle
 * claims pointer focus, so clicking into a field does not focus it.
 */
async function typeInto(field: HTMLElement, text: string) {
  field.focus();
  await userEvent.keyboard(text);
}

async function openActions() {
  // Radix menus open on pointer or key events; jsdom handles the keyboard path reliably.
  (await screen.findByRole("button", { name: "Actions for Arbeit" })).focus();
  await userEvent.keyboard("{Enter}");
}

describe("mailbox list", () => {
  it("shows each mailbox with its sync status", async () => {
    mockMailboxApi({
      mailboxes: [
        testMailbox(),
        testMailbox({
          id: "0199b000-0000-7000-8000-000000000002",
          display_name: "Verein",
          address: "kasse@verein.example",
          status: { ...testMailbox().status, phase: "error", last_error: "authentication_failed" },
        }),
      ],
    });
    await renderApp("/settings/mailboxes");
    const list = await screen.findByRole("list", { name: "Mailboxes" });
    expect(within(list).getByText(/Up to date · 3 messages/)).toBeInTheDocument();
    expect(
      within(list).getByText("Error: Sign-in failed. Check the user name and password."),
    ).toBeInTheDocument();
  });

  it("removes a mailbox after confirmation", async () => {
    const calls = mockMailboxApi();
    await renderApp("/settings/mailboxes");
    await openActions();
    await userEvent.click(await screen.findByRole("menuitem", { name: "Remove" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove mailbox?" });
    expect(dialog).toHaveTextContent("Arbeit and its 3 messages");
    expect(calls.some((call) => call.method === "DELETE")).toBe(false);
    await userEvent.click(within(dialog).getByRole("button", { name: "Remove" }));
    await waitFor(() =>
      expect(calls).toContainEqual({
        method: "DELETE",
        path: `/api/mailboxes/${MAILBOX_ID}`,
        body: undefined,
      }),
    );
  });

  it("pauses syncing", async () => {
    const calls = mockMailboxApi();
    await renderApp("/settings/mailboxes");
    await openActions();
    await userEvent.click(await screen.findByRole("menuitem", { name: "Pause syncing" }));
    await waitFor(() =>
      expect(calls).toContainEqual({
        method: "PATCH",
        path: `/api/mailboxes/${MAILBOX_ID}`,
        body: { sync_enabled: false },
      }),
    );
  });

  it("offers adding the first mailbox", async () => {
    mockMailboxApi({ mailboxes: [] });
    await renderApp("/settings/mailboxes");
    expect(await screen.findByText("No mailboxes yet")).toBeInTheDocument();
  });

  it("forwards the result of the Gmail connect flow", async () => {
    mockMailboxApi();
    const { router } = await renderApp("/?mailbox_error=access_denied");
    await waitFor(() => expect(router.state.location.pathname).toBe("/settings/mailboxes"));
  });
});

describe("add mailbox", () => {
  it("shows only the IMAP form when no OAuth provider is configured", async () => {
    mockMailboxApi();
    await renderApp("/settings/mailboxes/new");
    expect(await screen.findByRole("form", { name: "IMAP account" })).toBeInTheDocument();
    expect(screen.queryByRole("radiogroup")).not.toBeInTheDocument();
  });

  it("validates, discovers the server, reports a failed test and adds the mailbox", async () => {
    const calls = mockMailboxApi({
      test: { ok: false, error: "authentication_failed", folders: [] },
    });
    const { router } = await renderApp("/settings/mailboxes/new");
    const form = await screen.findByRole("form", { name: "IMAP account" });

    await userEvent.click(within(form).getByRole("button", { name: "Add mailbox" }));
    expect(screen.getByText("Enter an email address.")).toBeInTheDocument();
    expect(screen.getByText("Enter the password.")).toBeInTheDocument();
    expect(calls.some((call) => call.path === "/api/mailboxes" && call.method === "POST")).toBe(
      false,
    );

    await typeInto(screen.getByLabelText("Email address"), "erika@firma.example");
    await typeInto(screen.getByLabelText("Password"), "secret");
    await waitFor(() =>
      expect(screen.getByLabelText("IMAP server")).toHaveValue("imap.firma.example"),
    );
    expect(screen.getByText(/The server was guessed from the domain/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Test connection" }));
    expect(
      await screen.findByText("Sign-in failed. Check the user name and password."),
    ).toBeInTheDocument();

    await userEvent.click(within(form).getByRole("button", { name: "Add mailbox" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/settings/mailboxes"));
    expect(calls).toContainEqual({
      method: "POST",
      path: "/api/mailboxes",
      body: {
        type: "imap",
        address: "erika@firma.example",
        provider_settings: { host: "imap.firma.example", security: "tls", port: 993 },
        credentials: { password: "secret" },
        sync_enabled: true,
      },
    });
  });

  it("keeps server settings the user entered before a late suggestion arrives", async () => {
    let answer = () => {};
    const calls = mockMailboxApi({ discovered: new Promise((resolve) => (answer = resolve)) });
    await renderApp("/settings/mailboxes/new");
    await typeInto(await screen.findByLabelText("Email address"), "erika@firma.example");
    // Leaving the field starts autodiscovery; its answer is still outstanding.
    await typeInto(screen.getByLabelText("Password"), "secret");
    await waitFor(() =>
      expect(calls.some((call) => call.path === "/api/mailboxes/autodiscover")).toBe(true),
    );
    await typeInto(screen.getByLabelText("IMAP server"), "localhost");
    await typeInto(screen.getByLabelText("Port"), "1143");
    await userEvent.selectOptions(screen.getByLabelText("Encryption"), "starttls");

    answer();
    expect(await screen.findByText(/The server was guessed from the domain/)).toBeInTheDocument();
    expect(screen.getByLabelText("IMAP server")).toHaveValue("localhost");
    expect(screen.getByLabelText("Port")).toHaveValue("1143");
    expect(screen.getByLabelText("Encryption")).toHaveValue("starttls");
    expect(screen.queryByText("Settings guessed from the domain.")).not.toBeInTheDocument();
  });

  it("does not fill the server field the user is in when a late suggestion arrives", async () => {
    let answer = () => {};
    const calls = mockMailboxApi({ discovered: new Promise((resolve) => (answer = resolve)) });
    await renderApp("/settings/mailboxes/new");
    await typeInto(await screen.findByLabelText("Email address"), "erika@firma.example");
    await typeInto(screen.getByLabelText("Password"), "secret");
    await waitFor(() =>
      expect(calls.some((call) => call.path === "/api/mailboxes/autodiscover")).toBe(true),
    );
    const host = screen.getByLabelText("IMAP server");
    host.focus();

    answer();
    expect(await screen.findByText(/The server was guessed from the domain/)).toBeInTheDocument();
    expect(host).toHaveValue("");
    await userEvent.keyboard("localhost");
    expect(host).toHaveValue("localhost");
    expect(screen.getByLabelText("Port")).toHaveValue("");
  });

  it("shows the server's reason when adding fails", async () => {
    mockMailboxApi({
      create: problem(422, { error_code: "tls_certificate_invalid" }),
    });
    await renderApp("/settings/mailboxes/new");
    await typeInto(await screen.findByLabelText("Email address"), "erika@firma.example");
    await typeInto(screen.getByLabelText("Password"), "secret");
    await waitFor(() =>
      expect(screen.getByLabelText("IMAP server")).toHaveValue("imap.firma.example"),
    );
    await userEvent.click(screen.getByRole("button", { name: "Add mailbox" }));
    expect(await screen.findByText("The server's certificate is not valid.")).toBeInTheDocument();
  });

  it("connects OAuth providers through their sign-in flow", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    const calls = mockMailboxApi({
      providers: [
        { type: "imap", connect: "credentials", oauth_start_path: null },
        { type: "gmail", connect: "oauth", oauth_start_path: "/mail/gmail/oauth/start" },
      ],
    });
    await renderApp("/settings/mailboxes/new");
    await userEvent.click(await screen.findByRole("radio", { name: /Google/ }));
    await userEvent.click(screen.getByRole("button", { name: "Continue with Google" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("https://accounts.example/authorize"));
    expect(calls).toContainEqual({
      method: "POST",
      path: "/api/mail/gmail/oauth/start",
      body: { return_to: "/settings/mailboxes" },
    });
    expect(screen.queryByRole("radio", { name: /Microsoft 365/ })).not.toBeInTheDocument();
  });
});
