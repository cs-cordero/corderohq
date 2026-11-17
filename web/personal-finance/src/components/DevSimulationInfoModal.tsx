/**
 * Informational modal explaining the dev-mode date-simulation feature. Renders
 * once per 12 hours (per browser, per user via localStorage). Dev-only — no-op
 * in prod builds.
 *
 * Mounted in Layout so it appears after authentication (Layout is only reached
 * by signed-in routes; the callback / login flow doesn't render it).
 */

import { useEffect, useState } from "react";
import { IS_DEV } from "../dev";

const LS_KEY = "personalFinance.devSimInfoShownAt";
const REAPPEAR_AFTER_MS = 12 * 60 * 60 * 1000;

function shouldShow(): boolean {
    if (!IS_DEV) return false;
    try {
        const raw = window.localStorage.getItem(LS_KEY);
        if (!raw) return true;
        const last = new Date(raw).getTime();
        if (Number.isNaN(last)) return true;
        return Date.now() - last >= REAPPEAR_AFTER_MS;
    } catch {
        return false;
    }
}

function markShown(): void {
    try {
        window.localStorage.setItem(LS_KEY, new Date().toISOString());
    } catch {
        /* ignore */
    }
}

export default function DevSimulationInfoModal() {
    // `visible` is the runtime gate; computed once at mount. We never re-show
    // within the same component lifecycle.
    const [visible, setVisible] = useState<boolean>(() => shouldShow());

    // Lock background scroll while the modal is up. Cheap UX nicety.
    useEffect(() => {
        if (!visible) return;
        const prev = document.body.style.overflow;
        document.body.style.overflow = "hidden";
        return () => {
            document.body.style.overflow = prev;
        };
    }, [visible]);

    if (!visible) return null;

    const dismiss = () => {
        markShown();
        setVisible(false);
    };

    return (
        <div className="dev-modal-backdrop" onClick={dismiss}>
            <div className="dev-modal" onClick={(e) => e.stopPropagation()}>
                <h2 className="dev-modal-title">Dev mode reminder</h2>
                <ol className="dev-modal-list">
                    <li>
                        Dev mode lets you simulate <strong>future dates</strong> via the date input in the top banner.
                        Use it to test month rollover, lock, and pin behavior.
                    </li>
                    <li>
                        Simulation has <strong>real side effects</strong>: visiting a budget or summary page under a
                        future date triggers densification, which writes <em>real</em> Budget rows for every month
                        between today and the simulated date. Those rows persist after you clear the sim.
                    </li>
                    <li>
                        To clean up after a sim session, run{" "}
                        <code>uv run python lambda/scripts/wipe_future_budget_rows.py --stage dev</code>. That deletes
                        Budget rows for months past the real current month.
                    </li>
                </ol>
                <div className="dev-modal-actions">
                    <button className="dev-btn dev-btn-primary" onClick={dismiss}>
                        Got it
                    </button>
                </div>
                <p className="dev-modal-footnote">This reminder reappears at most once every 12 hours.</p>
            </div>
        </div>
    );
}
