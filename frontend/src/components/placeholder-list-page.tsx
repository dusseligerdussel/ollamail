import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { Trans } from "react-i18next";

import { EmptyState } from "@/components/empty-state";
import { KeyHint } from "@/components/key-hint";
import { PageHeader } from "@/components/page-header";
import { SplitView } from "@/components/split-view";
import { useListNavigation } from "@/hooks/use-list-navigation";

interface PlaceholderListPageProps {
  id: string;
  title: string;
  icon: LucideIcon;
  emptyTitle: string;
  emptyDescription: string;
  action?: ReactNode;
  noSelection: string;
}

/** List/detail page without data yet. Replaced by the respective feature. */
export function PlaceholderListPage({
  id,
  title,
  icon,
  emptyTitle,
  emptyDescription,
  action,
  noSelection,
}: PlaceholderListPageProps) {
  useListNavigation({ count: 0 });

  return (
    <SplitView
      id={id}
      list={
        <>
          <PageHeader title={title} />
          <EmptyState
            icon={icon}
            title={emptyTitle}
            description={emptyDescription}
            action={action}
          />
        </>
      }
      detail={
        <>
          <div aria-hidden="true" className="h-header shrink-0 border-b" />
          <EmptyState
            title={noSelection}
            description={
              <Trans
                i18nKey="common.navigateHint"
                components={[<KeyHint key="j" keys="j" />, <KeyHint key="k" keys="k" />]}
              />
            }
          />
        </>
      }
    />
  );
}
