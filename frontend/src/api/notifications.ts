import { queryOptions, useMutation, useQueryClient } from "@tanstack/react-query";

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
