import type { ReactNode } from "react";

import { AppHeader } from "@/components/app-header";
import { OverviewProvider } from "@/components/overview-provider";
import { TooltipProvider } from "@/components/ui/tooltip";

/**
 * The app shell around every signed-in screen (DESIGN §4): the header with the mode,
 * freshness and kill switch, over one live Overview feed the screens share (D45).
 * The sign-in page is outside it.
 */
export default function AppLayout({ children }: { children: ReactNode }) {
  return (
    <OverviewProvider>
      <TooltipProvider>
        <AppHeader />
        <main className="mx-auto flex max-w-7xl flex-col gap-6 px-4 py-6">{children}</main>
      </TooltipProvider>
    </OverviewProvider>
  );
}
