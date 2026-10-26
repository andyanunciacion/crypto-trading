import type { Metadata } from "next";
import type { ReactNode } from "react";

import "./globals.css";

export const metadata: Metadata = {
  title: "crypto-trading",
};

// Runs before first paint, so a light-theme choice (ThemeToggle) doesn't flash dark first.
const THEME_SCRIPT = `try{if(localStorage.getItem("theme")==="light")document.documentElement.classList.remove("dark")}catch(e){}`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    // Dark by default (UX8). The script above may change the class before React loads,
    // hence suppressHydrationWarning (it covers this element's attributes only).
    <html lang="en" className="dark" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
