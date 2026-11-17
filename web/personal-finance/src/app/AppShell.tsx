import { useEffect } from "react";
import { Outlet, useLocation } from "react-router";
import { useAdmin } from "../admin";
import { getCurrentUserEmail, signOut } from "../auth";
import DevBanner from "../components/DevBanner";
import DevSimulationInfoModal from "../components/DevSimulationInfoModal";
import { readMonthParam } from "../hooks/useMonthParam";
import SidebarNav from "./SidebarNav";
import ThemeToggle from "./ThemeToggle";
import styles from "./AppShell.module.css";

// The admin override lasts for one page and one month. It goes off when the path or the month changes.
// A change of `?view=` on Allocations keeps it, because the two modes are one page.
function useOverrideReset(): void {
    const { setAdminModeOn } = useAdmin();
    const { pathname, search } = useLocation();
    const month = readMonthParam(new URLSearchParams(search));
    useEffect(() => {
        setAdminModeOn(false);
    }, [pathname, month, setAdminModeOn]);
}

export default function AppShell() {
    useOverrideReset();
    const email = getCurrentUserEmail();

    return (
        <>
            <DevSimulationInfoModal />
            <DevBanner />
            <div className={styles.app}>
                <nav aria-label="Main" className={styles.nav}>
                    <div className={styles.brandRow}>
                        <img src="/favicon.svg" alt="" className={styles.brandMark} />
                        <div className={styles.brand}>CorderoHQ</div>
                    </div>
                    <SidebarNav />
                    <div className={styles.navFoot}>
                        <ThemeToggle />
                        {email && <div className={styles.signedIn}>Signed in · {email}</div>}
                        <button type="button" className={styles.signOut} onClick={signOut}>
                            Sign out
                        </button>
                    </div>
                </nav>
                <main className={styles.main}>
                    <div className={styles.content}>
                        <Outlet />
                    </div>
                </main>
            </div>
        </>
    );
}
