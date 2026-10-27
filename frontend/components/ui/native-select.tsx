import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

/**
 * The browser's own <select>, styled like `Input`. Native, so the keyboard and screen
 * readers work as they do everywhere, with no extra dependency.
 */
function NativeSelect({ className, ...props }: ComponentProps<"select">) {
  return (
    <select
      data-slot="native-select"
      className={cn(
        "dark:bg-input/30 border-input bg-background flex h-9 w-full min-w-0 rounded-md border px-3 py-1 text-base shadow-xs transition-[color,box-shadow] outline-none disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50 md:text-sm",
        "focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px]",
        className,
      )}
      {...props}
    />
  );
}

export { NativeSelect };
