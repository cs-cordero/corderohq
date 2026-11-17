import { useState } from "react";
import { IS_DEV } from "../dev";
import { clearSimulatedDate, getSimulatedDate, setSimulatedDate } from "../simulated_date";

export default function DevBanner() {
    const current = getSimulatedDate();
    const [draft, setDraft] = useState<string>(current ?? "");

    if (!IS_DEV) return null;

    const simulating = current !== null;

    // Future-only: simulating into the past would let densification cold-start at
    // an earlier year-month and write spurious $0 rows that persist after the sim
    // clears. Backend rejects past dates anyway; this gives immediate UI feedback.
    const todayIso = new Date().toISOString().slice(0, 10);

    const apply = () => {
        const trimmed = draft.trim();
        if (!trimmed) return;
        if (trimmed < todayIso) {
            window.alert(`Simulated date must be today (${todayIso}) or later.`);
            return;
        }
        setSimulatedDate(trimmed);
    };

    return (
        <div className={`dev-banner${simulating ? " dev-banner-simulating" : ""}`}>
            <span className="dev-banner-text">DEV MODE</span>
            <label className="dev-banner-sim">
                Simulate date:
                <input
                    type="date"
                    min={todayIso}
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    onKeyDown={(e) => {
                        if (e.key === "Enter") apply();
                    }}
                />
                <button className="dev-btn" onClick={apply}>
                    Apply
                </button>
                {simulating && (
                    <>
                        <span className="dev-banner-sim-active">simulating {current}</span>
                        <button className="dev-btn" onClick={clearSimulatedDate}>
                            Clear
                        </button>
                    </>
                )}
            </label>
        </div>
    );
}
