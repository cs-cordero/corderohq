import { useCallback, useEffect, useState } from "react";
import { fetchAllCategories, fetchAllIncomeSources, fetchAuditLog } from "../api";
import LoadingOverlay from "../components/LoadingOverlay";
import StatusMessage from "../components/StatusMessage";
import { formatCurrency } from "../format";
import { useStatus } from "../hooks";
import type { AuditEntry, AuditUser, Category, IncomeSource } from "../types";

const DEFAULT_LIMIT = 10;

// Show one side of a {before, after} pair: an amount, a paycheck as its monthly net pay, or a
// one-time line as its amount.
function describeAuditValue(value: unknown): string {
    if (typeof value === "number") return formatCurrency(value);
    if (typeof value !== "object" || value === null) return "none";
    const obj = value as Record<string, unknown>;
    if (typeof obj.gross === "number" && typeof obj.paychecksPerMonth === "number" && Array.isArray(obj.deductions)) {
        let deductions = 0;
        for (const d of obj.deductions) {
            const amount: unknown = typeof d === "object" && d !== null ? (d as { amount?: unknown }).amount : null;
            if (typeof amount === "number") deductions += amount;
        }
        return `${formatCurrency((obj.gross - deductions) * obj.paychecksPerMonth)}/month net`;
    }
    if (typeof obj.amount === "number") return formatCurrency(obj.amount);
    return "none";
}
const MAX_LIMIT = 200;

export default function AuditLogPage() {
    const [entries, setEntries] = useState<AuditEntry[]>([]);
    const [categories, setCategories] = useState<Category[]>([]);
    const [incomeSources, setIncomeSources] = useState<IncomeSource[]>([]);
    const [limit, setLimit] = useState<number>(DEFAULT_LIMIT);
    const [limitInput, setLimitInput] = useState<string>(String(DEFAULT_LIMIT));
    const { status, showLoading, showError, clear } = useStatus();

    const load = useCallback(async () => {
        showLoading("Loading...");
        try {
            const [log, cats, sources] = await Promise.all([
                fetchAuditLog(limit),
                fetchAllCategories(),
                fetchAllIncomeSources(),
            ]);
            setEntries(log);
            setCategories(cats);
            setIncomeSources(sources);
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
    // An income plan entry has no category. A paycheck entry names its income source. A
    // one-time line can be deleted later, so its entry holds the label in `changes.line`.
    const renderIncome = (entry: AuditEntry) => {
        const source = incomeSources.find((s) => s.incomeSourceId === entry.incomeSourceId);
        if (source) return <span>Income: {source.label}</span>;
        const line = entry.changes.line;
        if (typeof line === "object" && line !== null) {
            const { before, after } = line as { before?: { label?: unknown }; after?: { label?: unknown } };
            const label = after?.label ?? before?.label;
            if (typeof label === "string") return <span>One-time income: {label}</span>;
        }
        return <span>Income</span>;
    };

    const renderCategory = (entry: AuditEntry) => {
        if (entry.categoryId === undefined) return renderIncome(entry);
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
        // CREATE / UPDATE / PIN / UNPIN — {before, after} per field. A paycheck shows as its
        // monthly net pay, and a one-time line as its amount.
        return Object.entries(entry.changes)
            .map(([field, value]) => {
                if (typeof value !== "object" || value === null) return `${field}: ${String(value)}`;
                const v = value as { before?: unknown; after?: unknown };
                const b = describeAuditValue(v.before);
                const a = describeAuditValue(v.after);
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
