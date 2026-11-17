import type { ReactNode } from "react";
import { Link } from "react-router";
import { useAdmin } from "../admin";
import { cx } from "../ui/cx";
import styles from "./LegacyPage.module.css";

export interface TemporaryLink {
    label: string;
    to: string;
    current?: boolean;
}

interface Props {
    children: ReactNode;
    // True for an old page with a locked month. An admin user then sees a temporary override button.
    adminOverride?: boolean;
    // Temporary links to old pages that the new sidebar does not show.
    links?: TemporaryLink[];
}

// Wrap an old page in `.legacy-page`, so the old rules apply to it and cannot touch the new shell. The
// checkpoint that replaces a page removes its wrapper.
export default function LegacyPage({ children, adminOverride = false, links = [] }: Props) {
    const { isAdminUser, adminModeOn, setAdminModeOn } = useAdmin();
    const showOverride = adminOverride && isAdminUser;
    return (
        <>
            {(links.length > 0 || showOverride) && (
                <div className={styles.bar}>
                    {links.map((link) => (
                        <Link
                            key={link.to}
                            to={link.to}
                            className={cx(styles.link, link.current && styles.current)}
                            aria-current={link.current ? "page" : undefined}
                        >
                            {link.label}
                        </Link>
                    ))}
                    {showOverride && (
                        <button
                            type="button"
                            className={styles.override}
                            aria-pressed={adminModeOn}
                            onClick={() => setAdminModeOn(!adminModeOn)}
                        >
                            {adminModeOn ? "Override on" : "Use admin override"}
                        </button>
                    )}
                </div>
            )}
            <div className="legacy-page">{children}</div>
        </>
    );
}
