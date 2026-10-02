import { useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { useCallback, useMemo } from "react";
import { useTranslation } from "react-i18next";

import {
  currentUserQueryOptions,
  logout,
  type ProfileUpdate,
  type User,
  updateProfile,
} from "@/api/auth";
import type { SupportedLanguage } from "@/i18n";

export interface CurrentUser extends User {
  isAdmin: boolean;
}

/**
 * The signed-in user (`GET /api/auth/me`). Only for pages behind the route guard in
 * `routes/__root.tsx`, which loads the user before any of them renders.
 */
export function useCurrentUser(): CurrentUser {
  const { data: user } = useSuspenseQuery(currentUserQueryOptions);
  const current = useMemo(() => user && { ...user, isAdmin: user.role === "admin" }, [user]);
  if (!current) throw new Error("useCurrentUser() requires a signed-in user");
  return current;
}

/** Changes display name, language or time zone of the own account. */
export function useUpdateProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ProfileUpdate) => updateProfile(body),
    onSuccess: (user) => queryClient.setQueryData(currentUserQueryOptions.queryKey, user),
  });
}

/**
 * Switches the interface language and stores it in the profile (used e.g. for notifications
 * and digests), so it also applies on other devices after the next sign-in.
 */
export function useChangeLanguage() {
  const { i18n } = useTranslation();
  const { mutate } = useUpdateProfile();
  return useCallback(
    (language: SupportedLanguage) => {
      void i18n.changeLanguage(language);
      mutate({ language });
    },
    [i18n, mutate],
  );
}

/** Ends the session; on success the login page is loaded from scratch. */
export function useLogout() {
  return useMutation({ mutationFn: logout });
}
