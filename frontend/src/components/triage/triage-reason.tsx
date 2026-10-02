import { Check, CircleAlert } from "lucide-react";
import type { ReactNode } from "react";
import { Trans, useTranslation } from "react-i18next";

import type { Category, Triage } from "@/api/triage";
import { KeyHint } from "@/components/key-hint";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { mediaQueries, useMediaQuery } from "@/hooks/use-media-query";

import { CHANGE_CATEGORY_KEYS, useCorrectCategory } from "./triage-controls";
import { useCategoryName, useMessageTriage } from "./use-triage";

const RULES = new Set([
  "list_unsubscribe",
  "precedence_bulk",
  "precedence_junk",
  "auto_submitted",
  "robot_sender",
]);

/**
 * One unobtrusive line above the thread: the category, why it was chosen, and a menu to
 * change it (also `c`, see `TriageControls`).
 */
export function TriageReason({ messageId }: { messageId: string }) {
  const { t } = useTranslation();
  const categoryName = useCategoryName();
  const { triage, category, categories, isPending } = useMessageTriage(messageId);

  // The message stays readable without categories; retrying is up to the user.
  if (categories.isError) {
    return <CategoriesError onRetry={() => void categories.refetch()} />;
  }
  if (isPending) return <Skeleton className="h-5 w-72 max-w-full" />;

  let text: ReactNode;
  if (!triage || !category) {
    text = t("triage.reason.pending");
  } else {
    const values = { category: categoryName(category) };
    const components = [<span key="category" className="font-medium text-foreground" />];
    if (category.hidden) {
      text = <Trans i18nKey="triage.reason.hidden" values={values} components={components} />;
    } else if (triage.source === "user") {
      text = <Trans i18nKey="triage.reason.user" values={values} components={components} />;
    } else if (triage.source === "sender_rule") {
      text = <Trans i18nKey="triage.reason.sender_rule" values={values} components={components} />;
    } else if (triage.source === "rule") {
      const rule = triage.rule && RULES.has(triage.rule) ? triage.rule : "other";
      text = (
        <Trans
          i18nKey="triage.reason.rule"
          values={{ ...values, rule: t(`triage.rules.${rule as "other"}`) }}
          components={components}
        />
      );
    } else if (triage.reason) {
      text = (
        <Trans
          i18nKey="triage.reason.llm"
          values={{ ...values, reason: triage.reason }}
          components={components}
        />
      );
    } else {
      text = <Trans i18nKey="triage.reason.llmNoReason" values={values} components={components} />;
    }
  }

  return (
    <div
      data-testid="triage-reason"
      className="flex items-start gap-3 px-1 text-ui text-muted-foreground"
    >
      <p className="min-w-0 flex-1 py-0.5">
        {text}
        {triage?.priority === 1 && category && !category.hidden && (
          <span className="text-foreground"> · {t("triage.highPriority")}</span>
        )}
      </p>
      <CategoryMenu
        messageId={messageId}
        triage={category ? triage : undefined}
        categories={categories.visible}
      />
    </div>
  );
}

function CategoriesError({ onRetry }: { onRetry: () => void }) {
  const { t } = useTranslation();
  return (
    <div
      data-testid="triage-reason"
      className="flex items-start gap-3 px-1 text-ui text-muted-foreground"
    >
      <p role="alert" className="flex min-w-0 flex-1 items-start gap-2 py-0.5">
        <CircleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-destructive" />
        {t("triage.categoriesError")}
      </p>
      <Button
        variant="ghost"
        size="xs"
        className="-my-0.5 shrink-0 text-muted-foreground"
        onClick={onRetry}
      >
        {t("triage.retry")}
      </Button>
    </div>
  );
}

/** Gets the categories as a prop: it mounts and unmounts with the skeleton above (#86). */
function CategoryMenu({
  messageId,
  triage,
  categories,
}: {
  messageId: string;
  triage: Triage | undefined;
  categories: Category[];
}) {
  const { t } = useTranslation();
  const categoryName = useCategoryName();
  const hasKeyboard = useMediaQuery(mediaQueries.keyboard);
  const correct = useCorrectCategory(messageId, triage);

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="xs" className="-my-0.5 shrink-0 text-muted-foreground">
          {triage ? t("triage.change") : t("triage.assign")}
          {hasKeyboard && (
            <span aria-hidden="true">
              <KeyHint keys={CHANGE_CATEGORY_KEYS} />
            </span>
          )}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-48">
        <DropdownMenuLabel className="text-xs font-medium text-muted-foreground">
          {t("triage.picker.title")}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        {categories.map((category) => (
          <DropdownMenuItem key={category.id} onSelect={() => correct(category)}>
            <span className="flex-1">{categoryName(category)}</span>
            {category.id === triage?.category_id && (
              <Check aria-hidden="true" className="text-brand" />
            )}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
