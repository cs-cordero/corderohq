import { useCallback, useEffect, useState } from "react";
import { fetchAllCategories, fetchAuditLog } from "../api";
import LoadingOverlay from "../components/LoadingOverlay";
import StatusMessage from "../components/StatusMessage";
import { formatCurrency } from "../format";
import { useStatus } from "../hooks";
import type { AuditEntry, AuditUser, Category } from "../types";

const DEFAULT_LIMIT = 10;
const MAX_LIMIT = 200;

export default function AuditLogPage() {
    const [entries, setEntries] = useState<AuditEntry[]>([]);
    const [categories, setCategories] = useState<Category[]>([]);
    const [limit, setLimit] = useState<number>(DEFAULT_LIMIT);
    const [limitInput, setLimitInput] = useState<string>(String(DEFAULT_LIMIT));
    const { status, showLoading, showError, clear } = useStatus();

    const load = useCallback(async () => {
        showLoading("Loading...");
        try {
            const [log, cats] = await Promise.all([fetchAuditLog(limit), fetchAllCategories()]);
            setEntries(log);
            setCategories(cats);
            clear();
        } catch (err) {
            showError(err);
        }
    }, [limit]); // eslint-disable-line react-hooks/exhaustive-deps

    useEffect(() => {
        load();
    }, [load]);

    const applyLimit = () => {
        const parsed = parseInt(limitInput, 10);
        if (Number.isNaN(parsed)) {
            setLimitInput(String(limit));
            return;
        }
        const clamped = Math.max(1, Math.min(MAX_LIMIT, parsed));
        setLimitInput(String(clamped));
        setLimit(clamped);
    };

    // CATEGORY_HARD_DELETE entries reference a category that no longer exists in the
    // Category table, so categoryName(id) wouldn't resolve. The audit row carries the
    // historical name in changes.name; we surface it here as a tooltip.
    const renderCategory = (entry: AuditEntry) => {
        if (entry.categoryId === undefined) return <span>Income</span>;
        const cat = categories.find((c) => c.categoryId === entry.categoryId);
        if (cat) return <span>{cat.name}</span>;
        const changesName = entry.changes.name;
        if (typeof changesName === "string") {
            return (
                <span title={`Deleted: ${changesName} (${entry.categoryId})`}>
                    <em>&lt;deleted: {changesName}&gt;</em>
                </span>
            );
        }
        return <span title={entry.categoryId}>&lt;deleted category&gt;</span>;
    };

    const formatChanges = (entry: AuditEntry) => {
        // CATEGORY_HARD_DELETE has a flat scalar payload, not before/after pairs.
        if (entry.action === "CATEGORY_HARD_DELETE") {
            const c = entry.changes;
            const bits: string[] = [];
            if (typeof c.budgetRowsDeleted === "number") bits.push(`budget rows: ${c.budgetRowsDeleted}`);
            if (typeof c.transactionsDeleted === "number") bits.push(`transactions: ${c.transactionsDeleted}`);
            return bits.join(", ");
        }
        // CREATE / UPDATE / PIN / UNPIN — {before, after} per field.
        return Object.entries(entry.changes)
            .map(([field, value]) => {
                if (typeof value !== "object" || value === null) return `${field}: ${String(value)}`;
                const v = value as { before?: number | null; after?: number | null };
                const b = typeof v.before === "number" ? formatCurrency(v.before) : "none";
                const a = typeof v.after === "number" ? formatCurrency(v.after) : "none";
                return `${field}: ${b} → ${a}`;
            })
            .join(", ");
    };

    const formatTimestamp = (iso: string) => new Date(iso).toLocaleString();

    const userLabel = (user: AuditUser) => user.email || user.username || user.sub || "unknown";
    const userTooltip = (user: AuditUser) => `sub: ${user.sub}\nusername: ${user.username}`;

    return (
        <div className="page">
            <h1>Audit Log</h1>
            <LoadingOverlay message={status.message} visible={status.type === "loading"} />
            <StatusMessage message={status.type !== "loading" ? status.message : ""} type={status.type} />

            <div className="audit-controls">
                <label>
                    Show last{" "}
                    <input
                        type="number"
                        min={1}
                        max={MAX_LIMIT}
                        value={limitInput}
                        onChange={(e) => setLimitInput(e.target.value)}
                        onBlur={applyLimit}
                        onKeyDown={(e) => {
                            if (e.key === "Enter") applyLimit();
                        }}
                    />{" "}
                    entries (max {MAX_LIMIT})
                </label>
            </div>

            <table className="data-table audit-table">
                <thead>
                    <tr>
                        <th>When</th>
                        <th>Action</th>
                        <th>Override</th>
                        <th>Budget Month</th>
                        <th>Category</th>
                        <th>Changes</th>
                        <th>Explanation</th>
                        <th>User</th>
                    </tr>
                </thead>
                <tbody>
                    {entries.map((entry) => (
                        <tr key={entry.sortId} className={entry.override ? "audit-override" : ""}>
                            <td className="nowrap">{formatTimestamp(entry.changedAt)}</td>
                            <td>
                                <span className={`badge badge-${entry.action.toLowerCase()}`}>{entry.action}</span>
                            </td>
                            <td>{entry.override && <span className="badge badge-override">override</span>}</td>
                            <td>{entry.effectiveYearMonth ?? "—"}</td>
                            <td>{renderCategory(entry)}</td>
                            <td className="changes">{formatChanges(entry)}</td>
                            <td>{entry.explanation}</td>
                            <td title={userTooltip(entry.user)}>{userLabel(entry.user)}</td>
                        </tr>
                    ))}
                    {entries.length === 0 && (
                        <tr>
                            <td colSpan={8} className="empty">
                                No audit entries yet
                            </td>
                        </tr>
                    )}
                </tbody>
            </table>
        </div>
    );
}
