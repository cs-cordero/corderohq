import { NavLink, useLocation } from "react-router";
import { useAdmin } from "../admin";
import { readMonthParam } from "../hooks/useMonthParam";
import { cx } from "../ui/cx";
import styles from "./AppShell.module.css";

interface NavItem {
    label: string;
    to: string;
    // True for Summary, Allocations, and Income. A link between two of these pages keeps the month.
    keepsMonth?: boolean;
    // True when only an exact path match makes the item active.
    end?: boolean;
    adminOnly?: boolean;
}

interface NavSection {
    title?: string;
    items: NavItem[];
}

const SECTIONS: NavSection[] = [
    {
        title: "Budget",
        items: [
            { label: "Summary", to: "/", keepsMonth: true, end: true },
            { label: "Allocations", to: "/allocations", keepsMonth: true },
            { label: "Income", to: "/income", keepsMonth: true },
            // Also active on /transactions/import.
            { label: "Transactions", to: "/transactions" },
        ],
    },
    {
        items: [
            // Also active on /net-worth/record.
            { label: "Net Worth", to: "/net-worth" },
            { label: "Accounts", to: "/accounts" },
        ],
    },
    {
        title: "System",
        items: [
            { label: "Household", to: "/household" },
            { label: "Audit log", to: "/audit-log" },
            { label: "Admin", to: "/admin", adminOnly: true },
        ],
    },
];

const MONTH_PAGES = new Set(
    SECTIONS.flatMap((s) => s.items)
        .filter((i) => i.keepsMonth)
        .map((i) => i.to),
);

export default function SidebarNav() {
    const { isAdminUser } = useAdmin();
    const location = useLocation();
    const month = MONTH_PAGES.has(location.pathname) ? readMonthParam(new URLSearchParams(location.search)) : null;

    return (
        <div className={styles.navSections}>
            {SECTIONS.map((section, index) => (
                <div key={section.title ?? index} className={styles.navSection}>
                    {section.title && <div className={styles.navHead}>{section.title}</div>}
                    <div className={cx(styles.navItems, section.title && styles.navItemsTitled)}>
                        {section.items
                            .filter((item) => !item.adminOnly || isAdminUser)
                            .map((item) => (
                                <NavLink
                                    key={item.to}
                                    to={item.keepsMonth && month ? `${item.to}?month=${month}` : item.to}
                                    end={item.end}
                                    className={({ isActive }) =>
                                        cx(
                                            styles.navLink,
                                            section.title && styles.navLinkTitled,
                                            isActive && styles.active,
                                        )
                                    }
                                >
                                    {item.label}
                                </NavLink>
                            ))}
                    </div>
                </div>
            ))}
        </div>
    );
}
