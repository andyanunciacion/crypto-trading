import type { Metadata } from "next";
import type { ReactNode } from "react";

import "./globals.css";

export const metadata: Metadata = {
  title: "crypto-trading",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    // Dark by default (UX8); the light toggle comes with the app shell (P5-2).
    <html lang="en" className="dark">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
