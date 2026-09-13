import type { Metadata, Viewport } from "next";
import "./globals.css";
import { PwaRegistration } from "./pwa-client";

export const metadata: Metadata = {
  title: "Cortex — Research Workspace",
  description:
    "A local product prototype for evidence-grounded research runs and human decisions.",
  applicationName: "Cortex",
  manifest: "/manifest.webmanifest",
  formatDetection: { telephone: false },
  appleWebApp: {
    capable: true,
    statusBarStyle: "black-translucent",
    title: "Cortex",
  },
  icons: {
    icon: [
      { url: "/icons/cortex-192.png", sizes: "192x192", type: "image/png" },
      { url: "/icons/cortex-512.png", sizes: "512x512", type: "image/png" },
    ],
    apple: [{ url: "/icons/cortex-180.png", sizes: "180x180", type: "image/png" }],
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
    { media: "(prefers-color-scheme: dark)", color: "#0a0a0a" },
  ],
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <head>
        <meta
          content="width=device-width, initial-scale=1, viewport-fit=cover"
          name="viewport"
        />
      </head>
      <body className="antialiased">
        <PwaRegistration />
        {children}
      </body>
    </html>
  );
}
