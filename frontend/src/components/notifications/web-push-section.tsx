import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Monitor, Smartphone } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  type PushDevice,
  registerPushDevice,
  removePushDevice,
  useRemovePushDevice,
  webPushKeys,
  webPushQueryOptions,
} from "@/api/notifications";
import { useDateFormat } from "@/components/account/sessions-list";
import { InlineError } from "@/components/inline-error";
import { Section, SwitchRow } from "@/components/notifications/settings-rows";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { requestNotificationAccess } from "@/lib/browser-notifications";
import {
  currentPushSubscription,
  rememberDevice,
  subscribePush,
  subscriptionBody,
  unsubscribePush,
  webPushReady,
} from "@/lib/web-push";

/** `undefined` while checking, `null` if this browser does not receive pushes. */
type ThisDevice = string | null | undefined;

function useDeviceName() {
  const { t } = useTranslation();
  return (device: Pick<PushDevice, "browser" | "os">) => {
    if (device.browser && device.os) {
      return t("account.sessions.device", { browser: device.browser, os: device.os });
    }
    return device.browser ?? device.os ?? t("account.sessions.unknownDevice");
  };
}

/**
 * Web Push (#181): notifications on this device without an open tab, and the list of the
 * user's devices. Hidden unless the administrator switched Web Push on.
 */
export function WebPushSection({ enabled }: { enabled: boolean }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const webPush = useQuery(webPushQueryOptions);
  const [supported, setSupported] = useState<boolean>();
  const [thisDevice, setThisDevice] = useState<ThisDevice>();
  const [busy, setBusy] = useState(false);
  const publicKey = webPush.data?.public_key ?? null;

  // Is this browser subscribed? Then refresh its registration (keys may have changed).
  useEffect(() => {
    if (!publicKey) return;
    let cancelled = false;
    void (async () => {
      const ready = await webPushReady();
      if (cancelled) return;
      setSupported(ready);
      const subscription = ready ? await currentPushSubscription(publicKey) : undefined;
      if (!subscription) {
        if (!cancelled) setThisDevice(null);
        return;
      }
      try {
        const device = await registerPushDevice(subscriptionBody(subscription));
        if (cancelled) return;
        rememberDevice(device.id);
        setThisDevice(device.id);
        void queryClient.invalidateQueries({ queryKey: webPushKeys.all });
      } catch {
        if (!cancelled) setThisDevice(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [publicKey, queryClient]);

  if (webPush.isPending) return null;
  if (webPush.isError) {
    return (
      <Section id="notifications-push" title={t("notifications.push.title")} className="mt-8">
        <InlineError error={webPush.error} className="px-4 py-3.5" />
      </Section>
    );
  }
  const { available, devices } = webPush.data;
  // Off on this server: open tabs still notify; devices left over can be removed.
  if (!available && devices.length === 0) return null;

  const turnOn = async () => {
    if (!publicKey) return;
    // Pushes need the browser's permission to show notifications.
    if ((await requestNotificationAccess()) !== "granted") return;
    const subscription = await subscribePush(publicKey);
    const device = await registerPushDevice(subscriptionBody(subscription));
    rememberDevice(device.id);
    setThisDevice(device.id);
  };

  const turnOff = async () => {
    if (thisDevice) await removePushDevice(thisDevice).catch(() => undefined);
    await unsubscribePush();
    rememberDevice(null);
    setThisDevice(null);
  };

  const toggle = async (checked: boolean) => {
    setBusy(true);
    try {
      await (checked ? turnOn() : turnOff());
    } catch {
      toast.error(t("notifications.push.enableFailed"));
    } finally {
      setBusy(false);
      void queryClient.invalidateQueries({ queryKey: webPushKeys.all });
    }
  };

  const description =
    supported === false
      ? `${t("notifications.push.enabledDescription")} ${t("notifications.push.unsupported")}`
      : t("notifications.push.enabledDescription");

  return (
    <Section id="notifications-push" title={t("notifications.push.title")} className="mt-8">
      {available && (
        <>
          <SwitchRow
            id="notifications-push-enabled"
            label={t("notifications.push.enabled")}
            description={description}
            checked={!!thisDevice}
            disabled={!enabled || busy || !supported || thisDevice === undefined}
            onCheckedChange={(checked) => void toggle(checked)}
          />
          <p className="px-4 py-3.5 text-ui text-muted-foreground">
            {t("notifications.push.service")}
          </p>
        </>
      )}
      <DeviceList
        devices={devices}
        thisDevice={thisDevice}
        onRemoved={(id) => {
          if (id !== thisDevice) return;
          rememberDevice(null);
          setThisDevice(null);
          void unsubscribePush().catch(() => undefined);
        }}
      />
    </Section>
  );
}

function DeviceList({
  devices,
  thisDevice,
  onRemoved,
}: {
  devices: PushDevice[];
  thisDevice: ThisDevice;
  onRemoved: (id: string) => void;
}) {
  const { t } = useTranslation();
  if (devices.length === 0) {
    return (
      <p className="px-4 py-3.5 text-ui text-muted-foreground">
        {t("notifications.push.noDevices")}
      </p>
    );
  }
  // This device first, then the others in the order they were added.
  const sorted = [...devices].sort(
    (a, b) => Number(b.id === thisDevice) - Number(a.id === thisDevice),
  );
  return (
    <div>
      <h3 className="px-4 pt-3.5 text-ui font-medium">{t("notifications.push.devices")}</h3>
      <ul className="divide-y" aria-label={t("notifications.push.devices")}>
        {sorted.map((device) => (
          <DeviceRow
            key={device.id}
            device={device}
            current={device.id === thisDevice}
            onRemoved={onRemoved}
          />
        ))}
      </ul>
    </div>
  );
}

function DeviceRow({
  device,
  current,
  onRemoved,
}: {
  device: PushDevice;
  current: boolean;
  onRemoved: (id: string) => void;
}) {
  const { t } = useTranslation();
  const formatDate = useDateFormat();
  const deviceName = useDeviceName();
  const remove = useRemovePushDevice();
  const name = deviceName(device);
  const Icon = device.mobile ? Smartphone : Monitor;
  return (
    <li className="flex items-center gap-3 px-4 py-3">
      <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-ui font-medium">{name}</span>
          {current && (
            <Badge variant="secondary" className="shrink-0 font-normal">
              {t("notifications.push.thisDevice")}
            </Badge>
          )}
        </div>
        <div className="text-ui break-words text-muted-foreground">
          {t("notifications.push.since", {
            date: formatDate(device.created_at),
            service: device.push_service,
          })}
        </div>
      </div>
      <Button
        variant="ghost"
        size="sm"
        className="text-ui"
        disabled={remove.isPending}
        aria-label={t("notifications.push.removeLabel", { device: name })}
        onClick={() =>
          remove.mutate(device.id, {
            onSuccess: () => onRemoved(device.id),
            onError: () => toast.error(t("notifications.push.removeFailed")),
          })
        }
      >
        {t("notifications.push.remove")}
      </Button>
    </li>
  );
}
