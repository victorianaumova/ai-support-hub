import type { RouteObject } from "react-router";
import { RequireOperator } from "../auth/RequireOperator";
import { AREA_ROLES } from "../auth/session";
import { ConsoleLayout } from "../layouts/ConsoleLayout";
import { PublicLayout } from "../layouts/PublicLayout";
import { ChatPage } from "../chat/ChatPage";
import { AuditPage } from "../console/AuditPage";
import { DashboardPage } from "../console/DashboardPage";
import { KbPage } from "../console/KbPage";
import { QueuePage } from "../console/QueuePage";
import { SettingsPage } from "../console/SettingsPage";
import { TicketPage } from "../console/TicketPage";
import { HelpArticle } from "../help/HelpArticle";
import { ConsoleLogin } from "../pages/ConsoleLogin";
import { Landing } from "../pages/Landing";
import { NotFound } from "../pages/NotFound";
import { RouteError } from "./RouteError";

/** Routes of UI §1. */
export const routes: RouteObject[] = [
  {
    element: <PublicLayout />,
    errorElement: <RouteError />,
    children: [
      { path: "/", element: <Landing /> },
      { path: "/chat", element: <ChatPage /> },
      { path: "/help/:docId", element: <HelpArticle /> },
      { path: "/console/login", element: <ConsoleLogin /> },
      { path: "*", element: <NotFound /> },
    ],
  },
  {
    path: "/console",
    element: <ConsoleLayout />,
    errorElement: <RouteError />,
    children: [
      {
        index: true,
        element: (
          <RequireOperator roles={AREA_ROLES.queue}>
            <QueuePage />
          </RequireOperator>
        ),
      },
      {
        path: "tickets/:id",
        element: (
          <RequireOperator roles={AREA_ROLES.queue}>
            <TicketPage />
          </RequireOperator>
        ),
      },
      {
        path: "kb",
        element: (
          <RequireOperator roles={AREA_ROLES.kb}>
            <KbPage />
          </RequireOperator>
        ),
      },
      {
        path: "dashboard",
        element: (
          <RequireOperator roles={AREA_ROLES.dashboard}>
            <DashboardPage />
          </RequireOperator>
        ),
      },
      {
        path: "settings",
        element: (
          <RequireOperator roles={AREA_ROLES.settings}>
            <SettingsPage />
          </RequireOperator>
        ),
      },
      {
        path: "audit",
        element: (
          <RequireOperator roles={AREA_ROLES.audit}>
            <AuditPage />
          </RequireOperator>
        ),
      },
    ],
  },
];
