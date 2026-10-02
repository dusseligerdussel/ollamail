import { Outlet } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";
import { useDefaultLayout } from "react-resizable-panels";

import { CloudNotice } from "@/components/cloud-notice";
import { CommandPalette } from "@/components/command-palette/command-palette";
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "@/components/ui/resizable";
import { Toaster } from "@/components/ui/sonner";
import { EventsListener } from "@/hooks/use-events";
import { mediaQueries, useMediaQuery } from "@/hooks/use-media-query";

import { BottomBar } from "./bottom-bar";
import { GlobalKeyboard } from "./global-keyboard";
import { ShortcutsOverlay, ShortcutsOverlayProvider } from "./shortcuts-overlay";
import { Sidebar } from "./sidebar";

function Main() {
  return (
    <main
      id="main"
      tabIndex={-1}
      className="flex h-full min-h-0 min-w-0 flex-1 flex-col outline-none"
    >
      <CloudNotice />
      <Outlet />
    </main>
  );
}

function DesktopLayout() {
  const { t } = useTranslation();
  const layout = useDefaultLayout({ id: "ollamail.shell", panelIds: ["nav", "content"] });

  return (
    <ResizablePanelGroup
      orientation="horizontal"
      defaultLayout={layout.defaultLayout}
      onLayoutChanged={layout.onLayoutChanged}
    >
      <ResizablePanel
        id="nav"
        defaultSize="232px"
        minSize="200px"
        maxSize="320px"
        groupResizeBehavior="preserve-pixel-size"
      >
        <Sidebar />
      </ResizablePanel>
      <ResizableHandle aria-label={t("shell.resizeNavigation")} />
      <ResizablePanel id="content" minSize="320px">
        <Main />
      </ResizablePanel>
    </ResizablePanelGroup>
  );
}

export function AppShell() {
  const { t } = useTranslation();
  const wide = useMediaQuery(mediaQueries.sidebar);

  return (
    <ShortcutsOverlayProvider>
      <a
        href="#main"
        className="sr-only z-50 rounded-md bg-background px-3 py-2 text-ui shadow-md focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:ring-2 focus:ring-ring"
      >
        {t("shell.skipToContent")}
      </a>
      <div className="flex h-dvh flex-col overflow-hidden">
        {wide ? (
          <DesktopLayout />
        ) : (
          <>
            <Main />
            <BottomBar />
          </>
        )}
      </div>
      <GlobalKeyboard />
      <CommandPalette />
      <ShortcutsOverlay />
      <EventsListener />
      <Toaster position={wide ? "bottom-right" : "top-center"} />
    </ShortcutsOverlayProvider>
  );
}
