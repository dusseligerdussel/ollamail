import { type FormEvent, useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";

import { FormField } from "@/components/form-field";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Textarea } from "@/components/ui/textarea";

export interface CategoryFormValues {
  name: string;
  description: string;
}

/** Name and description of a category, in a side sheet (add when `category` is null). */
export function CategorySheet({
  open,
  category,
  pending,
  renameHint,
  onOpenChange,
  onSubmit,
}: {
  open: boolean;
  category: CategoryFormValues | null;
  pending: boolean;
  /** Note that renaming loses the translation (built-in organisation categories). */
  renameHint?: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmit: (values: CategoryFormValues) => void;
}) {
  const { t } = useTranslation();
  const id = useId();
  const editing = category !== null;
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [submitted, setSubmitted] = useState(false);

  useEffect(() => {
    if (open) {
      setName(category?.name ?? "");
      setDescription(category?.description ?? "");
      setSubmitted(false);
    }
  }, [open, category]);

  const title = t(editing ? "triage.form.editTitle" : "triage.form.addTitle");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    setSubmitted(true);
    if (!name.trim()) return;
    onSubmit({ name: name.trim(), description: description.trim() });
  };

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        closeLabel={t("common.close")}
        className="w-full gap-0 sm:max-w-md"
        aria-describedby={`${id}-intro`}
      >
        <SheetHeader className="border-b">
          <SheetTitle>{title}</SheetTitle>
          <SheetDescription id={`${id}-intro`}>{t("triage.form.intro")}</SheetDescription>
        </SheetHeader>
        <form
          noValidate
          onSubmit={submit}
          aria-label={title}
          className="flex min-h-0 flex-1 flex-col"
        >
          <div className="flex flex-1 flex-col gap-4 overflow-y-auto p-4">
            <FormField
              label={t("triage.form.name")}
              value={name}
              onChange={(event) => setName(event.target.value)}
              maxLength={100}
              required
              autoFocus
              description={renameHint ? t("triage.form.renameHint") : undefined}
              error={submitted && !name.trim() ? t("triage.form.nameRequired") : undefined}
            />
            <div className="flex flex-col gap-2">
              <Label htmlFor={`${id}-description`} className="text-ui">
                {t("triage.form.description")}
              </Label>
              <Textarea
                id={`${id}-description`}
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                maxLength={2000}
                rows={5}
                aria-describedby={`${id}-description-hint`}
              />
              <p id={`${id}-description-hint`} className="text-xs text-muted-foreground">
                {t("triage.form.descriptionHint")}
              </p>
            </div>
          </div>
          <SheetFooter className="flex-row justify-end border-t">
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              {t("triage.form.cancel")}
            </Button>
            <Button type="submit" disabled={pending}>
              {t("triage.form.save")}
            </Button>
          </SheetFooter>
        </form>
      </SheetContent>
    </Sheet>
  );
}
