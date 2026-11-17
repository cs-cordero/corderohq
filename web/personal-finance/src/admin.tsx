/**
 * Admin-mode context. Tracks two things:
 *
 * - `isAdminUser` — derived from the JWT `cognito:groups` claim. The user has
 *   admin capability if `budget-admin-{stage}` is in their groups. The provider
 *   reads the groups when it mounts, and again when `refreshAdminUser` runs. The
 *   callback page runs it after a sign-in, because the provider mounts before the
 *   tokens exist. (Backend re-verifies on every request, so nothing
 *   security-sensitive depends on this client-side flag.)
 *
 * - `adminModeOn` — the admin override, defaults to off, never persisted. The
 *   "Use admin override" button of a locked month turns it on. `AppShell` turns it
 *   off when the path or the month changes.
 */

import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import { getCurrentUserGroups } from "./auth";
import { getConfig } from "./config";

interface AdminContextValue {
    isAdminUser: boolean;
    adminModeOn: boolean;
    setAdminModeOn: (on: boolean) => void;
    refreshAdminUser: () => void;
}

const AdminContext = createContext<AdminContextValue | null>(null);

function readIsAdminUser(): boolean {
    return getCurrentUserGroups().includes(`budget-admin-${getConfig().stage}`);
}

export function AdminProvider({ children }: { children: ReactNode }) {
    const [adminModeOn, setAdminModeOn] = useState(false);
    const [isAdminUser, setIsAdminUser] = useState(readIsAdminUser);
    const refreshAdminUser = useCallback(() => setIsAdminUser(readIsAdminUser()), []);

    return (
        <AdminContext.Provider value={{ isAdminUser, adminModeOn, setAdminModeOn, refreshAdminUser }}>
            {children}
        </AdminContext.Provider>
    );
}

export function useAdmin(): AdminContextValue {
    const ctx = useContext(AdminContext);
    if (!ctx) {
        throw new Error("useAdmin must be used inside <AdminProvider>");
    }
    return ctx;
}
