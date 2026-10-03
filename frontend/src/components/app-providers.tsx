import type { ReactNode } from "react";

import { ReauthProvider } from "@/components/auth/reauth";
import { CommandProvider } from "@/components/command-palette/command-provider";
import { ShortcutProvider } from "@/components/shortcuts/shortcut-provider";
import { ThemeProvider } from "@/components/theme-provider";

/** Context providers that do not depend on the router. */
export function AppProviders({ children }: { children: ReactNode }) {
  return (
    <ThemeProvider>
      <ShortcutProvider>
        <CommandProvider>
          <ReauthProvider>{children}</ReauthProvider>
        </CommandProvider>
      </ShortcutProvider>
    </ThemeProvider>
  );
}
