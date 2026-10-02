import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Digest } from "@/api/digest";
import { digestId, testDigest, testDigestSettings, testVoices } from "@/test/digest";
import { backend, json, mockFetch, problem } from "@/test/fetch";
import { messageId, testMailbox } from "@/test/mail";
import { setViewportWidth } from "@/test/media";
import { renderApp } from "@/test/render-app";

/** Digest endpoints on top of the default backend; records write requests. */
function mockDigestApi({
  digests = [] as Digest[],
  settings = testDigestSettings(),
  created = testDigest(9, { status: "pending", script: null, references: [] }),
} = {}) {
  const writes: { route: string; body: unknown }[] = [];
  const base = backend();
  const fetchMock = mockFetch(async (request) => {
    const url = new URL(request.url);
    const route = `${request.method} ${url.pathname}`;
    if (request.method !== "GET") {
      const text = await request.text();
      writes.push({ route, body: text ? JSON.parse(text) : undefined });
    }
    switch (route) {
      case "GET /api/digests":
        return json(digests);
      case "POST /api/digests":
        return json(created, { status: 202 });
      case "GET /api/digests/settings":
        return json(settings);
      case "PATCH /api/digests/settings":
        return json({ ...settings, ...(writes.at(-1)?.body as object) });
      case "GET /api/digests/voices":
        return json(testVoices);
      case "POST /api/digests/feed":
        return json(
          {
            feed_url: "https://mail.example.org/api/feeds/TEST-TOKEN.xml",
            created_at: "2026-10-02T09:00:00Z",
          },
          { status: 201 },
        );
      case "DELETE /api/digests/feed":
        return new Response(null, { status: 204 });
      case "GET /api/mailboxes":
        return json([
          testMailbox(),
          testMailbox({
            id: "0199b000-0000-7000-8000-000000000002",
            display_name: "Privat",
            address: "private@example.org",
          }),
        ]);
    }
    const detail = /^GET \/api\/digests\/([^/]+)$/.exec(route);
    if (detail) {
      const digest = [...digests, created].find((item) => item.id === detail[1]);
      return digest ? json(digest) : problem(404);
    }
    return base(request);
  });
  return { writes, fetchMock };
}

/** jsdom has no media playback; this double keeps play state and position per element. */
function stubAudio() {
  const state = new WeakMap<HTMLMediaElement, { paused: boolean; time: number }>();
  const get = (element: HTMLMediaElement) => {
    let entry = state.get(element);
    if (!entry) {
      entry = { paused: true, time: 0 };
      state.set(element, entry);
    }
    return entry;
  };
  const proto = HTMLMediaElement.prototype;
  const spies = [
    vi.spyOn(proto, "play").mockImplementation(function (this: HTMLMediaElement) {
      get(this).paused = false;
      this.dispatchEvent(new Event("play"));
      return Promise.resolve();
    }),
    vi.spyOn(proto, "pause").mockImplementation(function (this: HTMLMediaElement) {
      get(this).paused = true;
      this.dispatchEvent(new Event("pause"));
    }),
    vi.spyOn(proto, "canPlayType").mockReturnValue("probably"),
    vi.spyOn(proto, "paused", "get").mockImplementation(function (this: HTMLMediaElement) {
      return get(this).paused;
    }),
    vi.spyOn(proto, "currentTime", "get").mockImplementation(function (this: HTMLMediaElement) {
      return get(this).time;
    }),
    vi.spyOn(proto, "currentTime", "set").mockImplementation(function (
      this: HTMLMediaElement,
      value: number,
    ) {
      get(this).time = value;
      this.dispatchEvent(new Event("timeupdate"));
    }),
  ];
  return () => {
    for (const spy of spies) spy.mockRestore();
  };
}

function stubMediaSession() {
  const handlers = new Map<string, MediaSessionActionHandler | null>();
  const session = {
    metadata: null as MediaMetadata | null,
    playbackState: "none",
    setActionHandler: vi.fn((action: string, handler: MediaSessionActionHandler | null) =>
      handlers.set(action, handler),
    ),
    setPositionState: vi.fn(),
  };
  Object.defineProperty(navigator, "mediaSession", { configurable: true, value: session });
  vi.stubGlobal(
    "MediaMetadata",
    class {
      constructor(public init: MediaMetadataInit) {}
      get title() {
        return this.init.title;
      }
    },
  );
  return { session, handlers };
}

let restoreAudio: () => void;
beforeEach(() => {
  restoreAudio = stubAudio();
});
afterEach(() => {
  restoreAudio();
  Reflect.deleteProperty(navigator, "mediaSession");
});

function audio() {
  const element = document.querySelector("audio");
  if (!element) throw new Error("no audio element");
  return element;
}

describe("digest page", () => {
  it("shows an empty state with generate and settings actions", async () => {
    const { writes } = mockDigestApi();
    await renderApp("/digest");

    const empty = (await screen.findByText("No digest yet")).parentElement as HTMLElement;
    expect(within(empty).getByRole("link", { name: "Settings" })).toHaveAttribute(
      "href",
      "/digest/settings",
    );
    await userEvent.click(within(empty).getByRole("button", { name: "Generate now" }));

    await waitFor(() => expect(writes).toEqual([{ route: "POST /api/digests", body: undefined }]));
    expect(await screen.findAllByText("Waiting to be processed …")).not.toHaveLength(0);
  });

  it("shows the current digest with transcript links and the archive", async () => {
    mockDigestApi({ digests: [testDigest(2), testDigest(1, { title: "Older digest" })] });
    await renderApp("/digest");

    expect(await screen.findByRole("heading", { name: "Current" })).toBeInTheDocument();
    const archive = screen.getByRole("heading", { name: "Archive" }).closest("section");
    expect(within(archive as HTMLElement).getByText("Older digest")).toBeInTheDocument();

    const transcript = await screen.findByRole("region", { name: "Transcript" });
    expect(transcript).toHaveTextContent("The invoice is due on Monday");
    expect(within(transcript).getByRole("link", { name: "Open mail 1" })).toHaveAttribute(
      "href",
      `/inbox?message=${messageId(1)}`,
    );
    expect(within(transcript).queryByText("# Digest 2")).not.toBeInTheDocument();
  });

  it("explains a failed digest and offers to generate again", async () => {
    mockDigestApi({
      digests: [
        testDigest(1, {
          status: "failed",
          error_code: "tts_voice_not_available",
          script: null,
          audio_formats: [],
        }),
      ],
    });
    await renderApp("/digest");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The selected voice is not available.");
    expect(within(alert).getByRole("button", { name: "Generate again" })).toBeEnabled();
  });

  it("stacks list and detail on phones", async () => {
    setViewportWidth(390);
    mockDigestApi({ digests: [testDigest(1)] });
    const { router } = await renderApp("/digest");

    await userEvent.click(await screen.findByRole("link", { name: /Digest 1/ }));
    expect(await screen.findByRole("region", { name: "Transcript" })).toBeInTheDocument();
    expect(router.state.location.search).toEqual({ digest: digestId(1) });
    await userEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(await screen.findByRole("heading", { name: "Current" })).toBeInTheDocument();
  });
});

describe("digest player", () => {
  it("plays, skips and changes speed with buttons and keyboard", async () => {
    mockDigestApi({ digests: [testDigest(1)] });
    await renderApp("/digest");
    const player = await screen.findByRole("region", { name: "Playback" });

    await userEvent.click(within(player).getByRole("button", { name: "Play" }));
    expect(audio().src).toMatch(new RegExp(`/api/digests/${digestId(1)}/audio\\.opus$`));
    expect(within(player).getByRole("button", { name: "Pause" })).toBeInTheDocument();

    act(() => (document.activeElement as HTMLElement | null)?.blur());
    fireEvent.keyDown(window, { key: "ArrowRight" });
    fireEvent.keyDown(window, { key: "ArrowRight" });
    expect(audio().currentTime).toBe(30);
    fireEvent.keyDown(window, { key: "ArrowLeft" });
    expect(audio().currentTime).toBe(15);
    expect(within(player).getByRole("slider", { name: "Position" })).toHaveAttribute(
      "aria-valuetext",
      "0:15 of 2:05",
    );

    fireEvent.keyDown(window, { key: " " });
    expect(audio().paused).toBe(true);
    expect(within(player).getByRole("button", { name: "Play" })).toBeInTheDocument();

    // Space on a focused button is left to the button.
    const back = within(player).getByRole("button", { name: "Back 15 seconds" });
    act(() => back.focus());
    fireEvent.keyDown(back, { key: " " });
    expect(audio().paused).toBe(true);
    act(() => back.blur());

    fireEvent.keyDown(window, { key: ">" });
    expect(audio().playbackRate).toBe(1.25);
    expect(within(player).getByRole("button", { name: "Speed: 1.25×" })).toBeInTheDocument();

    // Radix menus open reliably via the keyboard in jsdom.
    within(player).getByRole("button", { name: "Speed: 1.25×" }).focus();
    await userEvent.keyboard("{Enter}");
    await userEvent.click(await screen.findByRole("menuitemradio", { name: "2×" }));
    expect(audio().playbackRate).toBe(2);
  });

  it("connects to the Media Session API", async () => {
    const { session, handlers } = stubMediaSession();
    mockDigestApi({ digests: [testDigest(1)] });
    await renderApp("/digest");
    const player = await screen.findByRole("region", { name: "Playback" });

    await userEvent.click(within(player).getByRole("button", { name: "Play" }));

    await waitFor(() => expect(session.metadata?.title).toBe("Digest 1"));
    expect(session.playbackState).toBe("playing");
    handlers.get("seekforward")?.({ action: "seekforward" });
    expect(audio().currentTime).toBe(15);
    handlers.get("seekto")?.({ action: "seekto", seekTime: 60 });
    expect(audio().currentTime).toBe(60);
    handlers.get("pause")?.({ action: "pause" });
    await waitFor(() => expect(session.playbackState).toBe("paused"));
    expect(session.setPositionState).toHaveBeenCalled();
  });
});

describe("digest settings", () => {
  it("saves schedule and content changes", async () => {
    const { writes } = mockDigestApi();
    await renderApp("/digest/settings");

    await userEvent.click(await screen.findByRole("switch", { name: "Daily digest" }));
    await waitFor(() => expect(writes.at(-1)?.body).toEqual({ enabled: true }));

    await userEvent.click(screen.getByRole("button", { name: "Saturday" }));
    await waitFor(() => expect(writes.at(-1)?.body).toEqual({ weekdays: [0, 1, 2, 3, 4, 6] }));

    await userEvent.click(screen.getByRole("radio", { name: "Short" }));
    await waitFor(() => expect(writes.at(-1)?.body).toEqual({ length: "short" }));

    const voice = screen.getByRole("combobox", { name: "Voice" });
    await waitFor(() => expect(within(voice).getByText("Amy · basic")).toBeInTheDocument());
    expect(within(voice).queryByText(/Thorsten/)).not.toBeInTheDocument();
    await userEvent.selectOptions(voice, "en_US-amy-low");
    await waitFor(() => expect(writes.at(-1)?.body).toEqual({ voice: "en_US-amy-low" }));

    await userEvent.click(await screen.findByRole("checkbox", { name: /Privat/ }));
    await waitFor(() =>
      expect(writes.at(-1)?.body).toEqual({
        mailbox_ids: ["0199b000-0000-7000-8000-000000000001"],
      }),
    );

    const time = screen.getByLabelText("Time");
    fireEvent.change(time, { target: { value: "06:30" } });
    fireEvent.blur(time);
    await waitFor(() => expect(writes.at(-1)?.body).toEqual({ delivery_time: "06:30:00" }));
  });

  it("creates the feed URL once, with copy and QR code", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    mockDigestApi();
    await renderApp("/digest/settings");

    expect(await screen.findByText(/Anyone who knows the feed URL/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Create feed URL" }));

    const url = await screen.findByRole("textbox", { name: "Feed URL" });
    expect(url).toHaveValue("https://mail.example.org/api/feeds/TEST-TOKEN.xml");
    expect(screen.getByRole("img", { name: "QR code of the feed URL" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Copy" }));
    expect(writeText).toHaveBeenCalledWith("https://mail.example.org/api/feeds/TEST-TOKEN.xml");
  });

  it("asks before replacing or turning off an active feed", async () => {
    const { writes } = mockDigestApi({
      settings: testDigestSettings({ feed: { active: true, created_at: "2026-09-01T08:00:00Z" } }),
    });
    await renderApp("/digest/settings");

    await userEvent.click(await screen.findByRole("button", { name: "Create new URL" }));
    expect(screen.getByRole("alert")).toHaveTextContent("The previous URL stops working");
    expect(writes).toEqual([]);
    await userEvent.click(
      within(screen.getByRole("alert")).getByRole("button", { name: "Create new URL" }),
    );
    expect(await screen.findByRole("textbox", { name: "Feed URL" })).toBeInTheDocument();
    expect(writes).toEqual([{ route: "POST /api/digests/feed", body: undefined }]);

    await userEvent.click(screen.getByRole("button", { name: "Turn off feed" }));
    await userEvent.click(
      within(screen.getByRole("alert")).getByRole("button", { name: "Turn off feed" }),
    );
    await waitFor(() => expect(writes.at(-1)?.route).toBe("DELETE /api/digests/feed"));
    expect(screen.queryByRole("textbox", { name: "Feed URL" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create feed URL" })).toBeInTheDocument();
  });
});
