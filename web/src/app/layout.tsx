import type { Metadata } from "next";
import "@fontsource-variable/instrument-sans/wght.css";
import "@fontsource/instrument-serif/400-italic.css";
import "@fontsource-variable/jetbrains-mono/index.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "Agent Ops Lab",
  description:
    "Break a real server and watch Claude fix it. A live Ubuntu lab with real faults, an MCP server with a command allowlist, human approvals and an audit log. Built by Anthony Jaimez.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
