import { createFileRoute, Link } from "@tanstack/react-router";
import { ArrowLeft, Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  type Category,
  useCreateCategory,
  useDeleteCategory,
  useOrderCategories,
  useUpdateCategory,
} from "@/api/triage";
import { useCommands } from "@/components/command-palette/command-provider";
import { InlineError } from "@/components/inline-error";
import { ListSkeleton } from "@/components/list-skeleton";
import { PageHeader } from "@/components/page-header";
import { CategoryRow, DeleteCategoryDialog, moved } from "@/components/triage/category-rows";
import { type CategoryFormValues, CategorySheet } from "@/components/triage/category-sheet";
import { useCategories, useCategoryName } from "@/components/triage/use-triage";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import type { Command } from "@/lib/commands";

export const Route = createFileRoute("/settings_/categories")({
  component: CategoriesPage,
});

function CategoriesPage() {
  const { t } = useTranslation();
  const categoryName = useCategoryName();
  const categories = useCategories();
  const order = useOrderCategories();
  const create = useCreateCategory();
  const update = useUpdateCategory();
  const remove = useDeleteCategory();
  // `null`: add a category; a category: edit it; `undefined`: closed.
  const [editing, setEditing] = useState<Category | null>();
  const [deleting, setDeleting] = useState<Category>();

  const list = categories.data ?? [];
  const visibleCount = categories.visible.length;

  const commands = useMemo<Command[]>(
    () => [
      {
        id: "triage.addCategory",
        label: t("triage.settings.add"),
        group: "actions",
        icon: Plus,
        run: () => setEditing(null),
      },
    ],
    [t],
  );
  useCommands(commands);

  const save = (values: CategoryFormValues) => {
    const done = (message: string) => {
      toast.success(message);
      setEditing(undefined);
    };
    if (editing) {
      update.mutate(
        { id: editing.id, body: values },
        { onSuccess: () => done(t("triage.form.updated")) },
      );
    } else {
      create.mutate(values, { onSuccess: () => done(t("triage.form.created")) });
    }
  };

  return (
    <>
      <PageHeader
        title={t("triage.settings.title")}
        leading={
          <Button asChild size="icon-sm" variant="ghost">
            <Link to="/settings" aria-label={t("triage.settings.back")}>
              <ArrowLeft />
            </Link>
          </Button>
        }
        actions={
          <Button size="sm" onClick={() => setEditing(null)}>
            <Plus />
            {t("triage.settings.add")}
          </Button>
        }
      />
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-2xl px-4 py-6 md:px-6 md:py-8">
          <p className="mb-4 text-ui text-muted-foreground">{t("triage.settings.intro")}</p>
          {categories.isPending && (
            <div className="rounded-lg border">
              <ListSkeleton rows={7} />
            </div>
          )}
          {categories.isError && (
            <InlineError
              error={categories.error}
              onRetry={categories.refetch}
              retrying={categories.isFetching}
            />
          )}
          {list.length > 0 && (
            <ul aria-label={t("triage.settings.title")} className="divide-y rounded-lg border">
              {list.map((category, index) => {
                const name = categoryName(category);
                const own = category.scope === "user";
                const lastVisible = !category.hidden && visibleCount <= 1;
                return (
                  <CategoryRow
                    key={category.id}
                    name={name}
                    description={category.description}
                    tag={
                      category.hidden
                        ? t("triage.settings.hiddenState")
                        : own
                          ? undefined
                          : t("triage.settings.organization")
                    }
                    muted={category.hidden}
                    first={index === 0}
                    last={index === list.length - 1}
                    disabled={order.isPending}
                    onMove={(offset) =>
                      order.mutate(moved(list, index, offset).map((item) => item.id))
                    }
                    onEdit={own ? () => setEditing(category) : undefined}
                    onDelete={own ? () => setDeleting(category) : undefined}
                  >
                    <Switch
                      className="mx-1.5"
                      checked={!category.hidden}
                      disabled={lastVisible || update.isPending}
                      title={lastVisible ? t("triage.settings.lastVisible") : undefined}
                      aria-label={t("triage.settings.visible", { category: name })}
                      onCheckedChange={(checked) =>
                        update.mutate({ id: category.id, body: { hidden: !checked } })
                      }
                    />
                  </CategoryRow>
                );
              })}
            </ul>
          )}
          {list.some((category) => category.scope === "organization") && (
            <p className="mt-4 text-ui text-muted-foreground">
              {t("triage.settings.organizationHint")}
            </p>
          )}
        </div>
      </div>
      <CategorySheet
        open={editing !== undefined}
        category={editing ?? null}
        pending={create.isPending || update.isPending}
        onOpenChange={(open) => !open && setEditing(undefined)}
        onSubmit={save}
      />
      <DeleteCategoryDialog
        open={!!deleting}
        description={t("triage.settings.deleteDescription", {
          category: deleting ? categoryName(deleting) : "",
        })}
        pending={remove.isPending}
        onClose={() => setDeleting(undefined)}
        onConfirm={() =>
          deleting &&
          remove.mutate(deleting.id, {
            onSuccess: () => {
              toast.success(t("triage.settings.deleted"));
              setDeleting(undefined);
            },
          })
        }
      />
    </>
  );
}
