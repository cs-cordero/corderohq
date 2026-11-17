import { Navigate, useSearchParams, type RouteObject } from "react-router";
import AccountsPage from "../pages/AccountsPage";
import AdminToolsPage from "../pages/AdminToolsPage";
import AuditLogPage from "../pages/AuditLogPage";
import BudgetPage from "../pages/BudgetPage";
import CategoriesPage from "../pages/CategoriesPage";
import IncomePage from "../pages/IncomePage";
import NetWorthEntryPage from "../pages/NetWorthEntryPage";
import SummaryPage from "../pages/SummaryPage";
import TransactionsPage from "../pages/TransactionsPage";
import UploadPage from "../pages/UploadPage";
import AppShell from "./AppShell";
import CallbackPage from "./CallbackPage";
import LegacyPage from "./LegacyPage";

// One page with two modes: Allocate (the old Budget page) and Arrange (the old Categories page). CP4
// replaces it.
function AllocationsLegacy() {
    const [params] = useSearchParams();
    const arrange = params.get("view") === "arrange";
    return (
        <LegacyPage
            adminOverride={!arrange}
            links={[
                { label: "Allocate", to: "/allocations", current: !arrange },
                { label: "Arrange", to: "/allocations?view=arrange", current: arrange },
            ]}
        >
            {arrange ? <CategoriesPage /> : <BudgetPage />}
        </LegacyPage>
    );
}

// Keep the query of an old path, so that a link with `?month=` still opens that month.
function RedirectWithQuery({ to }: { to: string }) {
    const [params] = useSearchParams();
    const [path, extra] = to.split("?");
    const merged = new URLSearchParams(extra);
    params.forEach((value, key) => merged.set(key, value));
    const query = merged.toString();
    return <Navigate to={query ? `${path}?${query}` : path} replace />;
}

export const ROUTES: RouteObject[] = [
    {
        path: "/callback",
        element: (
            <LegacyPage>
                <CallbackPage />
            </LegacyPage>
        ),
    },
    {
        element: <AppShell />,
        children: [
            {
                index: true,
                element: (
                    <LegacyPage>
                        <SummaryPage />
                    </LegacyPage>
                ),
            },
            { path: "allocations", element: <AllocationsLegacy /> },
            {
                path: "income",
                element: (
                    <LegacyPage adminOverride>
                        <IncomePage />
                    </LegacyPage>
                ),
            },
            {
                path: "transactions",
                element: (
                    <LegacyPage adminOverride links={[{ label: "Import CSV", to: "/transactions/import" }]}>
                        <TransactionsPage />
                    </LegacyPage>
                ),
            },
            {
                path: "transactions/import",
                element: (
                    <LegacyPage adminOverride>
                        <UploadPage />
                    </LegacyPage>
                ),
            },
            {
                path: "net-worth",
                element: (
                    <LegacyPage>
                        <NetWorthEntryPage />
                    </LegacyPage>
                ),
            },
            // CP11 makes the Record month page.
            { path: "net-worth/record", element: <Navigate to="/net-worth" replace /> },
            {
                path: "accounts",
                element: (
                    <LegacyPage>
                        <AccountsPage />
                    </LegacyPage>
                ),
            },
            // CP9 makes the Household page. Until then the people editor is on the Accounts page.
            { path: "household", element: <Navigate to="/accounts" replace /> },
            {
                path: "audit-log",
                element: (
                    <LegacyPage>
                        <AuditLogPage />
                    </LegacyPage>
                ),
            },
            {
                path: "admin",
                element: (
                    <LegacyPage>
                        <AdminToolsPage />
                    </LegacyPage>
                ),
            },

            // The old paths.
            { path: "budget", element: <RedirectWithQuery to="/allocations" /> },
            { path: "categories", element: <RedirectWithQuery to="/allocations?view=arrange" /> },
            { path: "upload", element: <RedirectWithQuery to="/transactions/import" /> },

            { path: "*", element: <Navigate to="/" replace /> },
        ],
    },
];
