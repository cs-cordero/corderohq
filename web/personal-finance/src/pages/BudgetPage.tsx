import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { useAdmin } from "../admin";
import {
    fetchAllCategories,
    fetchBudget,
    fetchCategoryGroups,
    fetchIncomeMonth,
    pinBudget,
    replaceBudget,
} from "../api";
import LeftToAssign from "../components/LeftToAssign";
import LoadingOverlay from "../components/LoadingOverlay";
import MonthPicker from "../components/MonthPicker";
import StatusMessage from "../components/StatusMessage";
import { editability } from "../editability";
import { currentYearMonth, formatCurrency, parseCurrencyInput } from "../format";
import { useStatus } from "../hooks";
import type { BudgetTarget, Category, CategoryGroup } from "../types";

interface BudgetRow {
    categoryId: string;
    categoryName: string;
    amount: number;
    active: boolean;
    pinned: boolean;
    groupId: string;
    orderInGroup: number;
}

const MAX_EXPLANATION_LENGTH = 1000;

export default function BudgetPage() {
    const [month, setMonth] = useState(currentYearMonth);
    const [rows, setRows] = useState<BudgetRow[]>([]);
    const [categories, setCategories] = useState<Category[]>([]);
    const [groups, setGroups] = useState<CategoryGroup[]>([]);
    const [editingId, setEditingId] = useState<string | null>(null);
    const [editAmount, setEditAmount] = useState("");
    const [editExplanation, setEditExplanation] = useState("");
    const [plannedNetIncome, setPlannedNetIncome] = useState<number | null>(null);
    const { status, showLoading, showSuccess, showError, clear } = useStatus();
    const { adminModeOn } = useAdmin();

    const editState = editability(month);
    const isLocked = editState === "LOCKED";
    const isReadOnly = isLocked && !adminModeOn;
    const willOverride = isLocked && adminModeOn;
    // Future months (anything past the current month, while still EDITABLE) go through
    // /pin instead of /replace. The architecture forbids /replace there because future
    // months are sparse — only explicit pins exist as rows.
    const isFutureMonth = month > currentYearMonth();

    const load = useCallback(async () => {
        showLoading("Loading...");
        try {
            const [targets, cats, grps, income] = await Promise.all([
                fetchBudget(month),
                fetchAllCategories(),
                fetchCategoryGroups(),
                fetchIncomeMonth(month),
            ]);
            setCategories(cats);
            setGroups(grps);
            setPlannedNetIncome(income.plannedNetIncome);

            const targetMap = new Map<string, BudgetTarget>();
            for (const t of targets) targetMap.set(t.categoryId, t);

            const newRows: BudgetRow[] = cats
                .filter((cat) => cat.active || targetMap.has(cat.categoryId))
                .map((cat) => {
                    const target = targetMap.get(cat.categoryId);
                    return {
                        categoryId: cat.categoryId,
                        categoryName: cat.active ? cat.name : `${cat.name} (deactivated)`,
                        amount: target?.amount ?? 0,
                        active: cat.active,
                        pinned: target?.pinned ?? false,
                        groupId: cat.groupId,
                        orderInGroup: cat.orderInGroup,
                    };
                });
            setRows(newRows);
            setEditingId(null);
            clear();
        } catch (err) {
            showError(err);
        }
    }, [month]); // eslint-disable-line react-hooks/exhaustive-deps

    const groupedRows = useMemo(() => {
        const sortedGroups = [...groups].sort((a, b) => a.order - b.order);
        const buckets = new Map<string, { group: CategoryGroup | null; rows: BudgetRow[] }>();
        for (const g of sortedGroups) buckets.set(g.groupId, { group: g, rows: [] });
        buckets.set("", { group: null, rows: [] });
        for (const row of rows) {
            (buckets.get(row.groupId) ?? buckets.get("")!).rows.push(row);
        }
        for (const bucket of buckets.values()) {
            bucket.rows.sort((a, b) => a.orderInGroup - b.orderInGroup || a.categoryName.localeCompare(b.categoryName));
        }
        return Array.from(buckets.values()).filter((b) => b.rows.length > 0);
    }, [rows, groups]);

    useEffect(() => {
        load();
    }, [load]);

    // The live total of the targets, with the row that is open for edit at its typed value. The
    // left to assign line then moves while the user types. Only active categories count here,
    // the same as the summary for an editable month.
    const liveBudgeted = useMemo(() => {
        const typed = editingId === null ? null : parseCurrencyInput(editAmount);
        return rows
            .filter((r) => r.active)
            .reduce((sum, r) => sum + (r.categoryId === editingId && typed !== null ? typed : r.amount), 0);
    }, [rows, editingId, editAmount]);

    const startEdit = (row: BudgetRow) => {
        setEditingId(row.categoryId);
        setEditAmount(row.amount ? (row.amount / 1_000_000).toFixed(2) : "");
        setEditExplanation("");
    };

    const cancelEdit = () => {
        setEditingId(null);
        setEditAmount("");
        setEditExplanation("");
    };

    const handleSaveRow = async (row: BudgetRow) => {
        const newAmount = parseCurrencyInput(editAmount);
        if (newAmount === null) {
            showError("Invalid amount");
            return;
        }
        const explanation = editExplanation.trim();
        if (!explanation) {
            showError("Explanation is required");
            return;
        }

        showLoading("Saving...");
        try {
            if (isFutureMonth) {
                const result = await pinBudget(month, [{ categoryId: row.categoryId, amount: newAmount }], explanation);
                setRows((prev) =>
                    prev.map((r) => (r.categoryId === row.categoryId ? { ...r, amount: newAmount, pinned: true } : r)),
                );
                cancelEdit();

                const { downstreamPins, pinMatchesCarriedValue } = result.warnings;
                const downstreamForThisCat = downstreamPins.filter((p) => p.categoryId === row.categoryId);
                if (downstreamForThisCat.length > 0) {
                    const monthsList = downstreamForThisCat.map((p) => p.yearMonth).join(", ");
                    const shouldClear = window.confirm(
                        `This category has pins in later months that won't see your new value: ${monthsList}. ` +
                            `Clear those downstream pins so the new value carries forward?`,
                    );
                    if (shouldClear) {
                        await pinBudget(
                            month,
                            downstreamForThisCat.map((p) => ({ categoryId: p.categoryId, amount: null })),
                            `Clearing downstream pins after pinning ${row.categoryName} ${monthsList}`,
                        );
                        // Reload to show the cleared downstream pins.
                        await load();
                        showSuccess("Pin saved; downstream pins cleared");
                    } else {
                        showSuccess("Pin saved; downstream pins kept");
                    }
                } else if (pinMatchesCarriedValue.includes(row.categoryId)) {
                    showSuccess(
                        "Pin saved — note: this matches the value that would have carried forward without a pin.",
                    );
                } else {
                    showSuccess("Pin saved");
                }
            } else {
                const targets = rows.map((r) => ({
                    categoryId: r.categoryId,
                    amount: r.categoryId === row.categoryId ? newAmount : r.amount,
                }));
                await replaceBudget(month, targets, explanation, willOverride);
                setRows((prev) => prev.map((r) => (r.categoryId === row.categoryId ? { ...r, amount: newAmount } : r)));
                cancelEdit();
                showSuccess(willOverride ? "Saved with override" : "Saved");
            }
            setTimeout(clear, 4000);
        } catch (err) {
            showError(err);
        }
    };

    const handleRemovePin = async (row: BudgetRow) => {
        if (
            !window.confirm(`Remove the pin for ${row.categoryName}? The value will carry forward from earlier months.`)
        ) {
            return;
        }
        showLoading("Removing pin...");
        try {
            await pinBudget(
                month,
                [{ categoryId: row.categoryId, amount: null }],
                `Removed pin for ${row.categoryName}`,
            );
            await load();
            showSuccess("Pin removed");
            setTimeout(clear, 3000);
        } catch (err) {
            showError(err);
        }
    };

    const saveLabel = (() => {
        if (isFutureMonth) return "Pin";
        if (willOverride) return "Save with override";
        return "Save";
    })();

    return (
        <div className="page">
            <h1>Monthly Budget</h1>
            <LoadingOverlay message={status.message} visible={status.type === "loading"} />
            <MonthPicker value={month} onChange={setMonth} />
            <StatusMessage message={status.type !== "loading" ? status.message : ""} type={status.type} />

            {plannedNetIncome !== null && <LeftToAssign plannedNetIncome={plannedNetIncome} budgeted={liveBudgeted} />}

            {isReadOnly && <p className="hint">This month is locked. Toggle Admin mode to edit with override.</p>}
            {isFutureMonth && (
                <p className="hint">
                    Future month: amounts carry forward from earlier months. Edits create explicit pins.
                </p>
            )}

            {categories.length === 0 && !status.message && (
                <p className="hint">No categories yet. Create some in the Categories page first.</p>
            )}

            {rows.length > 0 && (
                <table className="data-table">
                    <thead>
                        <tr>
                            <th>Category</th>
                            <th className="num">Amount</th>
                            <th>{isReadOnly ? "" : "Actions"}</th>
                        </tr>
                    </thead>
                    <tbody>
                        {groupedRows.map(({ group, rows: groupRows }) => (
                            <Fragment key={group?.groupId ?? "ungrouped"}>
                                <tr className="budget-group-header">
                                    <td colSpan={3}>{group ? group.name : "Ungrouped"}</td>
                                </tr>
                                {groupRows.map((row) => {
                                    const isEditing = editingId === row.categoryId;
                                    return (
                                        <tr key={row.categoryId} className={row.active ? "" : "faded"}>
                                            <td>
                                                {row.categoryName}
                                                {row.pinned && <span className="badge badge-pinned">pinned</span>}
                                            </td>
                                            <td className="num">
                                                {isEditing ? (
                                                    <input
                                                        type="text"
                                                        className="currency-input"
                                                        value={editAmount}
                                                        onChange={(e) => setEditAmount(e.target.value)}
                                                        placeholder="0.00"
                                                        autoFocus
                                                        onKeyDown={(e) => {
                                                            if (e.key === "Escape") cancelEdit();
                                                        }}
                                                    />
                                                ) : (
                                                    formatCurrency(row.amount)
                                                )}
                                            </td>
                                            <td>
                                                {isEditing ? (
                                                    <div className="edit-actions">
                                                        <input
                                                            type="text"
                                                            className="explanation-inline"
                                                            value={editExplanation}
                                                            onChange={(e) => setEditExplanation(e.target.value)}
                                                            placeholder="Explanation (required)"
                                                            maxLength={MAX_EXPLANATION_LENGTH}
                                                            onKeyDown={(e) => {
                                                                if (e.key === "Escape") cancelEdit();
                                                                if (e.key === "Enter") handleSaveRow(row);
                                                            }}
                                                        />
                                                        <button
                                                            className="small-btn"
                                                            onClick={() => handleSaveRow(row)}
                                                        >
                                                            {saveLabel}
                                                        </button>
                                                        <button className="small-btn" onClick={cancelEdit}>
                                                            Cancel
                                                        </button>
                                                    </div>
                                                ) : !isReadOnly && row.active ? (
                                                    <div className="edit-actions">
                                                        <button className="small-btn" onClick={() => startEdit(row)}>
                                                            {isFutureMonth ? (row.pinned ? "Edit pin" : "Pin") : "Edit"}
                                                        </button>
                                                        {isFutureMonth && row.pinned && (
                                                            <button
                                                                className="small-btn"
                                                                onClick={() => handleRemovePin(row)}
                                                            >
                                                                Remove pin
                                                            </button>
                                                        )}
                                                    </div>
                                                ) : null}
                                            </td>
                                        </tr>
                                    );
                                })}
                            </Fragment>
                        ))}
                    </tbody>
                </table>
            )}
        </div>
    );
}
