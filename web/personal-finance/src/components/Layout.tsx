import { useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useAdmin } from "../admin";
import { signOut } from "../auth";
import AdminBanner from "./AdminBanner";
import DevBanner from "./DevBanner";
import DevSimulationInfoModal from "./DevSimulationInfoModal";

type NavGroup = "budget" | "networth";
interface SubLink {
    to: string;
    label: string;
    end?: boolean;
}

const BUDGET_LINKS: SubLink[] = [
    { to: "/", label: "Summary", end: true },
    { to: "/budget", label: "Allocations" },
    { to: "/income", label: "Income" },
    { to: "/transactions", label: "Transactions" },
    { to: "/upload", label: "Upload" },
    { to: "/categories", label: "Categories" },
    { to: "/audit-log", label: "Audit Log" },
];

const NETWORTH_LINKS: SubLink[] = [
    { to: "/net-worth", label: "History" },
    { to: "/accounts", label: "Accounts" },
];

// The landing page for each group when you click into it from another section.
const GROUP_HOME: Record<NavGroup, string> = { budget: "/", networth: "/net-worth" };

// Find which top-level group holds a path. This sets which button looks active, and which
// sub-menu opens first. Check the net worth paths first. Summary ("/") is a budget path, so
// it must not match every other path as a prefix.
function groupForPath(pathname: string): NavGroup | null {
    if (NETWORTH_LINKS.some((l) => l.to === pathname)) return "networth";
    if (pathname === "/" || BUDGET_LINKS.some((l) => l.to === pathname)) return "budget";
    return null; // e.g. /admin
}

export default function Layout() {
    const { isAdminUser, adminModeOn, setAdminModeOn } = useAdmin();
    const location = useLocation();
    const navigate = useNavigate();
    const activeGroup = groupForPath(location.pathname);
    // The group whose sub-bar is open. It starts as the group of the current route, so the
    // correct sub-menu is there when the page opens.
    const [openGroup, setOpenGroup] = useState<NavGroup | null>(activeGroup);

    // A click on the group you are in opens or closes its sub-bar, and moves to no other
    // page. A click on a different group opens the home page of that group, and its
    // sub-bar.
    const handleGroupClick = (group: NavGroup) => {
        if (activeGroup === group) {
            setOpenGroup((cur) => (cur === group ? null : group));
        } else {
            navigate(GROUP_HOME[group]);
            setOpenGroup(group);
        }
    };

    const groupBtnClass = (group: NavGroup) =>
        `nav-group-btn${activeGroup === group ? " active" : ""}${openGroup === group ? " open" : ""}`;

    const subLinks = openGroup === "budget" ? BUDGET_LINKS : openGroup === "networth" ? NETWORTH_LINKS : null;

    return (
        <div className={`app${adminModeOn ? " admin-mode" : ""}`}>
            <DevSimulationInfoModal />
            <DevBanner />
            {adminModeOn && <AdminBanner onTurnOff={() => setAdminModeOn(false)} />}
            <header>
                <nav>
                    <span className="logo">Personal Finance</span>
                    <button
                        type="button"
                        className={groupBtnClass("budget")}
                        onClick={() => handleGroupClick("budget")}
                    >
                        Budget
                    </button>
                    <button
                        type="button"
                        className={groupBtnClass("networth")}
                        onClick={() => handleGroupClick("networth")}
                    >
                        Net Worth
                    </button>
                    <div className="nav-right">
                        {isAdminUser && <NavLink to="/admin">Admin</NavLink>}
                        {isAdminUser && (
                            <label className="admin-toggle" title="Toggle admin mode">
                                <input
                                    type="checkbox"
                                    checked={adminModeOn}
                                    onChange={(e) => setAdminModeOn(e.target.checked)}
                                />
                                <span>Admin</span>
                            </label>
                        )}
                        <button className="sign-out-btn" onClick={signOut}>
                            Sign Out
                        </button>
                    </div>
                </nav>
            </header>
            {subLinks && (
                <div className="subnav">
                    <nav className="subnav-inner">
                        {subLinks.map((link) => (
                            <NavLink key={link.to} to={link.to} end={link.end}>
                                {link.label}
                            </NavLink>
                        ))}
                    </nav>
                </div>
            )}
            <main>
                <Outlet />
            </main>
        </div>
    );
}
