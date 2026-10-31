import { useCallback, useEffect, useState } from "react";
import { fetchAllCategories, fetchAllIncomeSources, fetchAuditLog } from "../api";
import LoadingOverlay from "../components/LoadingOverlay";
import StatusMessage from "../components/StatusMessage";
import { formatCurrency } from "../format";
import { useStatus } from "../hooks";
import type { AuditEntry, AuditUser, Category, IncomeSource } from "../types";

const DEFAULT_LIMIT = 10;

// Show one side of a {before, after} pair: an amount, or an income as its label and the net amount
// of one payment. A recurring income also shows its end month.
function describeAuditValue(value: unknown): string {
    if (typeof value === "number") return formatCurrency(value);
    if (typeof value !== "object" || value === null) return "none";
    const obj = value as Record<string, unknown>;
    if (typeof obj.gross === "number" && Array.isArray(obj.deductions)) {
        let deductions = 0;
        for (const d of obj.deductions) {
            const amount: unknown = typeof d === "object" && d !== null ? (d as { amount?: unknown }).amount : null;
            if (typeof amount === "number") deductions += amount;
        }
        const label = typeof obj.label === "string" ? `${obj.label}, ` : "";
        const end = typeof obj.endMonth === "string" ? `, ends ${obj.endMonth}` : "";
        return `${label}${formatCurrency(obj.gross - deductions)} net per payment${end}`;
    }
    return "none";
}

// The label of the income in an income entry, from the value after the change, else before it.
function incomeLabelOf(entry: AuditEntry): string | null {
    for (const field of ["recurringIncome", "pinnedRecurringIncome", "oneTimeIncome"]) {
        const pair = entry.changes[field];
        if (typeof pair !== "object" || pair === null) continue;
        const { before, after } = pair as { before?: { label?: unknown }; after?: { label?: unknown } };
        const label = after?.label ?? before?.label;
        if (typeof label === "string") return label;
    }
    return null;
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
    // An income plan entry has no category. It names its income source when the income has one.
    // An income can be deleted later, so the entry holds the label of the income in `changes`.
    const renderIncome = (entry: AuditEntry) => {
        const source = incomeSources.find((s) => s.incomeSourceId === entry.incomeSourceId);
        const label = incomeLabelOf(entry);
        const kind = entry.plannedIncomeId?.startsWith("ONE#") ? "One-time income" : "Income";
        const parts = [source?.label, label].filter((part): part is string => part !== undefined && part !== null);
        return (
            <span title={entry.plannedIncomeId}>
                {kind}
                {parts.length > 0 ? `: ${parts.join(" — ")}` : ""}
            </span>
        );
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
        // Every other action has {before, after} for each field. An income shows its label and the
        // net amount of one payment.
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
