import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { useTheme } from "@/components/theme-provider";
import { findPassageRange } from "@/lib/find-passage";

/** Name of the CSS highlight that marks a passage the mail was opened at (search). */
const PASSAGE_HIGHLIGHT = "ollamail-passage";

/**
 * Content Security Policy of the mail document. The HTML is already sanitised by the server;
 * this is the second line of defence: no scripts, no frames, no forms, and images only from
 * the app itself (inline attachments) unless the user loaded external images.
 */
export function mailCsp(externalImages: boolean) {
  const images = externalImages ? "'self' data: https: http:" : "'self' data:";
  return [
    "default-src 'none'",
    `img-src ${images}`,
    "style-src 'unsafe-inline'",
    "font-src data:",
    "form-action 'none'",
    "base-uri 'none'",
  ].join("; ");
}

// Mail HTML is written for a light background, so it stays light in both themes: on the light
// page it sits flush with the text, in the dark theme it is shown as an inset sheet of paper.
const baseStyle = (paper: boolean) => `
  html { color-scheme: light; }
  body {
    margin: 0;
    padding: ${paper ? "16px" : "0"};
    font: 14px/1.5 "Inter Variable", ui-sans-serif, system-ui, sans-serif;
    color: #18181b;
    background: #ffffff;
    overflow-wrap: anywhere;
  }
  img { max-width: 100%; height: auto; }
  table { max-width: 100%; }
  pre { white-space: pre-wrap; }
  a { color: #1d4ed8; }
  blockquote { margin: 0 0 0 8px; padding-left: 8px; border-left: 2px solid #d4d4d8; color: #52525b; }
  ::highlight(${PASSAGE_HIGHLIGHT}) { background-color: #d6e4f7; color: inherit; }
`;

export function mailDocument(html: string, externalImages: boolean, paper = false) {
  return `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${mailCsp(externalImages)}">
<meta name="referrer" content="no-referrer">
<base target="_blank">
<style>${baseStyle(paper)}</style></head><body>${html}</body></html>`;
}

interface MailBodyFrameProps {
  /** Sanitised HTML from the server. */
  html: string;
  externalImages: boolean;
  title: string;
  /** Text to mark and scroll to, e.g. the passage a search hit or a cited source found. */
  passage?: string;
}

/** Marks `passage` in the mail document (CSS highlight, no DOM change) and returns its range. */
function markPassage(frame: HTMLIFrameElement, passage: string | undefined) {
  const view = frame.contentWindow as (Window & typeof globalThis) | null;
  const body = frame.contentDocument?.body;
  if (!view || !body) return undefined;
  const highlights = view.CSS?.highlights;
  highlights?.delete(PASSAGE_HIGHLIGHT);
  if (!passage) return undefined;
  const range = findPassageRange(body, passage);
  if (range && highlights && typeof view.Highlight === "function") {
    highlights.set(PASSAGE_HIGHLIGHT, new view.Highlight(range));
  }
  return range;
}

/**
 * Shows sanitised mail HTML in a sandboxed iframe: scripts, forms and plugins are disabled, links
 * open in a new tab without access to the app. `allow-same-origin` (without `allow-scripts`) lets
 * the app size the frame to its content and lets inline images load with the session cookie;
 * nothing inside the frame can run code.
 */
export function MailBodyFrame({ html, externalImages, title, passage }: MailBodyFrameProps) {
  const { t } = useTranslation();
  const paper = useTheme().resolvedTheme === "dark";
  const frame = useRef<HTMLIFrameElement>(null);
  const [height, setHeight] = useState(160);
  // Counts loads of the document, so the passage is marked again after a reload.
  const [loads, setLoads] = useState(0);
  const scrolledTo = useRef<string | undefined>(undefined);

  useEffect(() => {
    const element = frame.current;
    if (!element) return;
    let observer: ResizeObserver | undefined;
    const measure = () => {
      const body = element.contentDocument?.body;
      const root = element.contentDocument?.documentElement;
      if (!body || !root) return;
      setHeight(Math.max(root.scrollHeight, body.scrollHeight, 48));
    };
    const onLoad = () => {
      measure();
      setLoads((count) => count + 1);
      const body = element.contentDocument?.body;
      if (body && typeof ResizeObserver !== "undefined") {
        observer?.disconnect();
        observer = new ResizeObserver(measure);
        observer.observe(body);
      }
    };
    element.addEventListener("load", onLoad);
    return () => {
      element.removeEventListener("load", onLoad);
      observer?.disconnect();
    };
  }, []);

  // Runs after the frame got its full height, so the page can scroll to the passage.
  // biome-ignore lint/correctness/useExhaustiveDependencies: `height` re-runs it after resizing
  useEffect(() => {
    const element = frame.current;
    if (!element || loads === 0) return;
    const range = markPassage(element, passage);
    const key = passage && `${loads}:${passage}`;
    if (!range || !key || scrolledTo.current === key) return;
    scrolledTo.current = key;
    range.startContainer.parentElement?.scrollIntoView?.({ block: "center" });
  }, [loads, passage, height]);

  return (
    <iframe
      ref={frame}
      title={t("mail.bodyFrameTitle", { subject: title })}
      sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"
      referrerPolicy="no-referrer"
      srcDoc={mailDocument(html, externalImages, paper)}
      style={{ height }}
      className="block w-full rounded-md bg-white"
    />
  );
}
