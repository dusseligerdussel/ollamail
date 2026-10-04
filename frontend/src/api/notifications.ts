import { queryOptions, useMutation, useQueryClient } from "@tanstack/react-query";

import { rememberDevice, rememberedDevice, unsubscribePush } from "@/lib/web-push";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type NotificationSettings = components["schemas"]["NotificationSettingsRead"];
export type NotificationSettingsUpdate = components["schemas"]["NotificationSettingsUpdate"];
export type MailNotification = components["schemas"]["MailNotificationRead"];

/** Query keys. Not under `["message", …]`: mail events must not reload the settings. */
export const notificationKeys = {
  settings: ["notifications", "settings"] as const,
};

export const notificationSettingsQueryOptions = queryOptions({
  queryKey: notificationKeys.settings,
  queryFn: ({ signal }) => unwrap(api.GET("/notifications/settings", { signal })),
});

/** What the notification about a message shows (sender, category, subject if chosen). */
export function fetchMailNotification(messageId: string) {
  return unwrap(
    api.GET("/notifications/messages/{message_id}", {
      params: { path: { message_id: messageId } },
    }),
  );
}

export function useUpdateNotificationSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: NotificationSettingsUpdate) =>
      unwrap(api.PUT("/notifications/settings", { body })),
    onSuccess: (settings) => queryClient.setQueryData(notificationKeys.settings, settings),
  });
}

export type WebPush = components["schemas"]["WebPushRead"];
export type PushDevice = components["schemas"]["PushDeviceRead"];
export type PushSubscriptionBody = components["schemas"]["PushSubscriptionCreate"];

export const webPushKeys = {
  all: ["notifications", "push"] as const,
};

/** Whether Web Push is available, the server's public key and the user's devices. */
export const webPushQueryOptions = queryOptions({
  queryKey: webPushKeys.all,
  queryFn: ({ signal }) => unwrap(api.GET("/notifications/push", { signal })),
});

export function registerPushDevice(body: PushSubscriptionBody) {
  return unwrap(api.POST("/notifications/push/devices", { body }));
}

export function removePushDevice(deviceId: string) {
  return unwrap(
    api.DELETE("/notifications/push/devices/{device_id}", {
      params: { path: { device_id: deviceId } },
    }),
  );
}

export function useRemovePushDevice() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: removePushDevice,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: webPushKeys.all }),
  });
}

/**
 * Signing out: this browser stops receiving pushes for the user (the device is removed and
 * the browser's subscription ended). Best effort; never blocks signing out.
 */
export async function forgetPushDevice() {
  const id = rememberedDevice();
  rememberDevice(null);
  await Promise.allSettled([id ? removePushDevice(id) : undefined, unsubscribePush()]);
}
