import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useEffect } from "react";
import { useTranslation } from "react-i18next";

import { fetchMailNotification, notificationSettingsQueryOptions } from "@/api/notifications";
import { builtinCategoryKeys } from "@/api/triage";
import { useCategoryName } from "@/components/triage/use-triage";
import { type ServerEvent, subscribeServerEvents } from "@/hooks/use-events";
import {
  mailNotificationText,
  notificationAccess,
  pageIsInUse,
  showNotification,
} from "@/lib/browser-notifications";
import { sendServiceWorkerStrings } from "@/lib/web-push";

export const NOTIFICATION_EVENT = "notification.message";

function messageIdOf(event: ServerEvent): string | undefined {
  const ids = event.ids;
  if (typeof ids !== "object" || ids === null) return undefined;
  const id = (ids as Record<string, unknown>).message_id;
  return typeof id === "string" ? id : undefined;
}

/**
 * Shows a browser notification for each `notification.message` event (#149) while the app
 * is open but not in use. The server only sends the event to users who opted in; this
 * device additionally needs the browser's permission. Mount once inside the app shell.
 * Without an open tab, the service worker shows them (Web Push, #181).
 */
export function MailNotifier() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const categoryName = useCategoryName();

  // Notifications from Web Push are shown by the service worker (#181): it gets the texts in
  // the user's language from here (`t` changes with the language).
  useEffect(() => {
    void sendServiceWorkerStrings({
      unknownSender: t("notifications.unknownSender"),
      fallbackTitle: t("notifications.push.fallbackTitle"),
      fallbackBody: t("notifications.push.fallbackBody"),
      categories: Object.fromEntries(
        builtinCategoryKeys.map((key) => [key, t(`triage.builtin.${key}`)]),
      ),
    }).catch(() => undefined);
  }, [t]);

  useEffect(
    () =>
      subscribeServerEvents((event) => {
        if (event.type !== NOTIFICATION_EVENT) return;
        const messageId = messageIdOf(event);
        if (!messageId || notificationAccess() !== "granted" || pageIsInUse()) return;
        void (async () => {
          try {
            const settings = await queryClient.ensureQueryData(notificationSettingsQueryOptions);
            if (!settings.enabled) return;
            const content = await fetchMailNotification(messageId);
            showNotification({
              ...mailNotificationText({
                sender: content.sender,
                category: content.category ? categoryName(content.category) : null,
                subject: content.subject,
                unknownSender: t("notifications.unknownSender"),
              }),
              tag: `ollamail-message-${messageId}`,
              sound: content.sound,
              onClick: () => void navigate({ to: "/inbox", search: { message: messageId } }),
            });
          } catch {
            // Best effort: a mail that is gone or a failed request shows nothing.
          }
        })();
      }),
    [queryClient, navigate, categoryName, t],
  );
  return null;
}
