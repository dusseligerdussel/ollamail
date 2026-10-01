import type { ReactNode } from "react";
import { useDefaultLayout } from "react-resizable-panels";

import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "@/components/ui/resizable";
import { mediaQueries, useMediaQuery } from "@/hooks/use-media-query";

interface SplitViewProps {
  /** Persists the column widths per page. */
  id: string;
  list: ReactNode;
  detail: ReactNode;
  /** On narrow screens list and detail are stacked; this decides which one is shown. */
  detailOpen?: boolean;
}

/** List and detail column. Resizable side by side on wide screens, stacked on narrow ones. */
export function SplitView({ id, list, detail, detailOpen = false }: SplitViewProps) {
  const split = useMediaQuery(mediaQueries.split);
  const layout = useDefaultLayout({ id: `ollamail.split.${id}`, panelIds: ["list", "detail"] });

  if (!split) {
    return <div className="flex h-full min-h-0 flex-col">{detailOpen ? detail : list}</div>;
  }

  return (
    <ResizablePanelGroup
      orientation="horizontal"
      defaultLayout={layout.defaultLayout}
      onLayoutChanged={layout.onLayoutChanged}
    >
      <ResizablePanel id="list" defaultSize="40%" minSize="280px" maxSize="60%">
        <div className="flex h-full min-h-0 flex-col">{list}</div>
      </ResizablePanel>
      <ResizableHandle />
      <ResizablePanel id="detail" minSize="320px">
        <div className="flex h-full min-h-0 flex-col">{detail}</div>
      </ResizablePanel>
    </ResizablePanelGroup>
  );
}
