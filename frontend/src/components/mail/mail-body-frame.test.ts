import { describe, expect, it } from "vitest";

import { mailCsp, mailDocument } from "./mail-body-frame";

describe("mail document", () => {
  it("allows only own and inline images until external images are loaded", () => {
    expect(mailCsp(false)).toContain("img-src 'self' data:;");
    expect(mailCsp(false)).not.toContain("https:");
    expect(mailCsp(true)).toContain("img-src 'self' data: https: http:");
    for (const csp of [mailCsp(false), mailCsp(true)]) {
      expect(csp).toContain("default-src 'none'");
      expect(csp).not.toContain("script-src");
    }
  });

  it("embeds the policy, no referrer and opens links outside the app", () => {
    const html = mailDocument("<p>Hello</p>", false);
    expect(html).toContain('http-equiv="Content-Security-Policy"');
    expect(html).toContain('<meta name="referrer" content="no-referrer">');
    expect(html).toContain('<base target="_blank">');
    expect(html).toContain("<body><p>Hello</p></body>");
  });
});
