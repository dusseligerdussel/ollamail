export interface CurrentUser {
  id: string;
  isAdmin: boolean;
}

const placeholderUser: CurrentUser = { id: "placeholder", isAdmin: true };

/**
 * The signed-in user.
 *
 * Placeholder until authentication exists: always returns an admin so that the admin navigation
 * can be built and tested. Replaced by the real implementation from #11 / #12.
 */
export function useCurrentUser(): CurrentUser {
  return placeholderUser;
}
