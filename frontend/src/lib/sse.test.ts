import { describe, expect, it } from "vitest";

import { readServerSentEvents } from "./sse";

function streamOf(chunks: string[]) {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

async function collect(chunks: string[]) {
  const events = [];
  for await (const event of readServerSentEvents(streamOf(chunks))) events.push(event);
  return events;
}

describe("readServerSentEvents", () => {
  it("parses named events split across chunks", async () => {
    expect(
      await collect(['event: start\ndata: {"a"', ":1}\n\nevent: tok", 'en\ndata: {"b":2}\n\n']),
    ).toEqual([
      { event: "start", data: '{"a":1}' },
      { event: "token", data: '{"b":2}' },
    ]);
  });

  it("joins data lines, ignores comments and handles CRLF", async () => {
    expect(await collect([": ping\r\n\r\ndata: one\r\ndata: two\r\n\r\n"])).toEqual([
      { event: "message", data: "one\ntwo" },
    ]);
  });

  it("decodes multi-byte characters split between chunks", async () => {
    const bytes = new TextEncoder().encode("data: Grüße\n\n");
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes.slice(0, 9));
        controller.enqueue(bytes.slice(9));
        controller.close();
      },
    });
    const events = [];
    for await (const event of readServerSentEvents(stream)) events.push(event);
    expect(events).toEqual([{ event: "message", data: "Grüße" }]);
  });
});
