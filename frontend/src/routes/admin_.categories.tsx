import { useQuery } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import { ArrowLeft, Plus, Tags } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  type OrganizationCategory,
  organizationCategoriesQueryOptions,
  useCreateOrganizationCategory,
  useDeleteOrganizationCategory,
  useUpdateOrganizationCategory,
} from "@/api/triage";
import { useCommands } from "@/components/command-palette/command-provider";
import { EmptyState } from "@/components/empty-state";
import { Forbidden } from "@/components/forbidden";
import { InlineError } from "@/components/inline-error";
import { ListSkeleton } from "@/components/list-skeleton";
import { PageHeader } from "@/components/page-header";
import { CategoryRow, DeleteCategoryDialog, moved } from "@/components/triage/category-rows";
import { type CategoryFormValues, CategorySheet } from "@/components/triage/category-sheet";
import { useCategoryName } from "@/components/triage/use-triage";
import { Button } from "@/components/ui/button";
import { useCurrentUser } from "@/hooks/use-current-user";
import type { Command } from "@/lib/commands";

export const Route = createFileRoute("/admin_/categories")({
  component: OrganizationCategoriesPage,
});

function OrganizationCategoriesPage() {
  const { isAdmin } = useCurrentUser();
  // The API enforces permissions; this only avoids an empty page for other users.
  if (!isAdmin) return <Forbidden />;
  return <OrganizationCategories />;
}

function OrganizationCategories() {
  const { t } = useTranslation();
  const categoryName = useCategoryName();
  const categories = useQuery(organizationCategoriesQueryOptions);
  const create = useCreateOrganizationCategory();
  const update = useUpdateOrganizationCategory();
  const remove = useDeleteOrganizationCategory();
  const [editing, setEditing] = useState<OrganizationCategory | null>();
  const [deleting, setDeleting] = useState<OrganizationCategory>();
  const [moving, setMoving] = useState(false);
  const list = categories.data ?? [];
  const sheetCategory = useMemo(
    () => (editing ? { name: categoryName(editing), description: editing.description } : null),
    [editing, categoryName],
  );

  const commands = useMemo<Command[]>(
    () => [
      {
        id: "triage.addOrganizationCategory",
        label: t("triage.settings.add"),
        group: "actions",
        icon: Plus,
        run: () => setEditing(null),
      },
    ],
    [t],
  );
  useCommands(commands);

  // Positions are renumbered in list order; only changed ones are saved.
  const move = async (index: number, offset: -1 | 1) => {
    const next = moved(list, index, offset);
    setMoving(true);
    try {
      await Promise.all(
        next.flatMap((category, position) =>
          category.position === position
            ? []
            : [update.mutateAsync({ id: category.id, body: { position } })],
        ),
      );
    } catch {
      // Shown as a toast by the query client.
    } finally {
      setMoving(false);
    }
  };

  const save = (values: CategoryFormValues) => {
    const done = (message: string) => {
      toast.success(message);
      setEditing(undefined);
    };
    if (editing) {
      // An unchanged name keeps the translation of a built-in category.
      const body = {
        ...(values.name !== categoryName(editing) && { name: values.name }),
        description: values.description,
      };
      update.mutate({ id: editing.id, body }, { onSuccess: () => done(t("triage.form.updated")) });
    } else {
      const position = list.reduce((max, category) => Math.max(max, category.position + 1), 0);
      create.mutate({ ...values, position }, { onSuccess: () => done(t("triage.form.created")) });
    }
  };

  return (
    <>
      <PageHeader
        title={t("triage.admin.title")}
        leading={
          <Button asChild size="icon-sm" variant="ghost">
            <Link to="/admin" aria-label={t("triage.admin.back")}>
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
          <p className="mb-4 text-ui text-muted-foreground">{t("triage.admin.intro")}</p>
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
          {categories.data?.length === 0 && (
            <EmptyState
              icon={Tags}
              title={t("triage.admin.empty")}
              description={t("triage.admin.emptyDescription")}
              action={
                <Button size="sm" onClick={() => setEditing(null)}>
                  {t("triage.settings.add")}
                </Button>
              }
            />
          )}
          {list.length > 0 && (
            <ul aria-label={t("triage.admin.title")} className="divide-y rounded-lg border">
              {list.map((category, index) => (
                <CategoryRow
                  key={category.id}
                  name={categoryName(category)}
                  description={category.description}
                  first={index === 0}
                  last={index === list.length - 1}
                  disabled={moving}
                  onMove={(offset) => void move(index, offset)}
                  onEdit={() => setEditing(category)}
                  onDelete={() => setDeleting(category)}
                />
              ))}
            </ul>
          )}
        </div>
      </div>
      <CategorySheet
        open={editing !== undefined}
        category={sheetCategory}
        renameHint={!!editing?.builtin_key}
        pending={create.isPending || update.isPending}
        onOpenChange={(open) => !open && setEditing(undefined)}
        onSubmit={save}
      />
      <DeleteCategoryDialog
        open={!!deleting}
        description={t("triage.admin.deleteDescription", {
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
