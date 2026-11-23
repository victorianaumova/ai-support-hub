import type { RouteObject } from "react-router";
import { RequireOperator } from "../auth/RequireOperator";
import { AREA_ROLES } from "../auth/session";
import { ConsoleLayout } from "../layouts/ConsoleLayout";
import { PublicLayout } from "../layouts/PublicLayout";
import { ConsoleLogin } from "../pages/ConsoleLogin";
import { Landing } from "../pages/Landing";
import { NotFound } from "../pages/NotFound";
import { Placeholder } from "../pages/Placeholder";
import { RouteError } from "./RouteError";

/** Routes of UI §1. */
export const routes: RouteObject[] = [
  {
    element: <PublicLayout />,
    errorElement: <RouteError />,
    children: [
      { path: "/", element: <Landing /> },
      { path: "/chat", element: <Placeholder title="Customer chat" task="P10-02" /> },
      { path: "/help/:docId", element: <Placeholder title="Help article" task="P10-02" /> },
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
            <Placeholder title="Ticket queue" task="P10-03" />
          </RequireOperator>
        ),
      },
      {
        path: "tickets/:id",
        element: (
          <RequireOperator roles={AREA_ROLES.queue}>
            <Placeholder title="Ticket" task="P10-03" />
          </RequireOperator>
        ),
      },
      {
        path: "kb",
        element: (
          <RequireOperator roles={AREA_ROLES.kb}>
            <Placeholder title="Knowledge base" task="P10-04" />
          </RequireOperator>
        ),
      },
      {
        path: "dashboard",
        element: (
          <RequireOperator roles={AREA_ROLES.dashboard}>
            <Placeholder title="Dashboard" task="P10-04" />
          </RequireOperator>
        ),
      },
      {
        path: "settings",
        element: (
          <RequireOperator roles={AREA_ROLES.settings}>
            <Placeholder title="Settings" task="P10-04" />
          </RequireOperator>
        ),
      },
      {
        path: "audit",
        element: (
          <RequireOperator roles={AREA_ROLES.audit}>
            <Placeholder title="Audit log" task="P10-04" />
          </RequireOperator>
        ),
      },
    ],
  },
];
