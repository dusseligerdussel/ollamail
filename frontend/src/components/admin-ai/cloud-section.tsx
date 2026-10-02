import { TriangleAlert } from "lucide-react";
import { useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import { type AISettings, useUpdateAISettings } from "@/api/ai";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";

import { AdminSection } from "./section";

function Warning() {
  const { t } = useTranslation();
  return (
    <div className="flex items-start gap-2 text-ui">
      <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-foreground" />
      <div className="min-w-0">
        <p className="font-medium">{t("pages.ai.cloud.warningTitle")}</p>
        <p className="text-muted-foreground">{t("pages.ai.cloud.warning")}</p>
      </div>
    </div>
  );
}

/** Global switch for cloud providers; turning it on needs a confirmation. */
export function CloudSection({ settings }: { settings: AISettings }) {
  const { t } = useTranslation();
  const id = useId();
  const update = useUpdateAISettings();
  const [confirming, setConfirming] = useState(false);

  const save = (enabled: boolean) =>
    update.mutate(
      { cloud_enabled: enabled },
      {
        onSuccess: () => {
          setConfirming(false);
          toast.success(t(enabled ? "pages.ai.cloud.enabled" : "pages.ai.cloud.disabled"));
        },
      },
    );

  const checked = update.isPending
    ? Boolean(update.variables.cloud_enabled)
    : settings.cloud_enabled;
  return (
    <AdminSection id={`${id}-title`} title={t("pages.ai.cloud.section")}>
      <div className="flex items-center justify-between gap-6 px-4 py-3.5">
        <div className="min-w-0">
          <label htmlFor={`${id}-switch`} className="text-ui font-medium">
            {t("pages.ai.cloud.label")}
          </label>
          <p className="text-ui text-muted-foreground">{t("pages.ai.cloud.description")}</p>
        </div>
        <Switch
          id={`${id}-switch`}
          checked={checked}
          disabled={update.isPending}
          onCheckedChange={(value) => (value ? setConfirming(true) : save(false))}
        />
      </div>
      <div className="px-4 py-3.5">
        <Warning />
      </div>
      <Dialog open={confirming} onOpenChange={setConfirming}>
        <DialogContent closeLabel={t("common.close")}>
          <DialogHeader>
            <DialogTitle>{t("pages.ai.cloud.confirmTitle")}</DialogTitle>
            <DialogDescription>{t("pages.ai.cloud.warning")}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="outline">{t("pages.ai.cloud.cancel")}</Button>
            </DialogClose>
            <Button disabled={update.isPending} onClick={() => save(true)}>
              {t("pages.ai.cloud.confirm")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </AdminSection>
  );
}
