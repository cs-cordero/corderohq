import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { useAdmin } from "../admin";
import {
    fetchAllCategories,
    fetchBudget,
    fetchCategoryGroups,
    fetchIncomeMonth,
    replaceBudget,
    updateBudget,
} from "../api";
import LeftToAssign from "../components/LeftToAssign";
import ScopeChoice from "../components/ScopeChoice";
import LoadingOverlay from "../components/LoadingOverlay";
import MonthPicker from "../components/MonthPicker";
import StatusMessage from "../components/StatusMessage";
import { editability } from "../editability";
import { currentYearMonth, formatCurrency, parseCurrencyInput } from "../format";
import { useStatus } from "../hooks";
import type { BudgetTarget, Category, CategoryGroup, ChangeScope } from "../types";

interface BudgetRow {
    categoryId: string;
    categoryName: string;
    amount: number; // what the month shows: the pinned amount if any, else the carried amount
    carried: number; // the carried amount, which later months copy
    hasPinned: boolean; // the month has a pinned amount, which only this month uses
    active: boolean;
    ownCarried: boolean; // a future month holds its own carried amount ("changes here")
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
    const [editScope, setEditScope] = useState<ChangeScope>("FORWARD");
    const [plannedNetIncome, setPlannedNetIncome] = useState<number | null>(null);
    const { status, showLoading, showSuccess, showError, clear } = useStatus();
    const { adminModeOn } = useAdmin();

    const editState = editability(month);
    const isLocked = editState === "LOCKED";
    const isReadOnly = isLocked && !adminModeOn;
    const willOverride = isLocked && adminModeOn;
    // A future month (any month after the current month) goes through /update, not /replace. A
    // future month is sparse: it holds a row only where the user set a value.
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
                    const carried = target?.carriedAmount ?? 0;
                    return {
                        categoryId: cat.categoryId,
                        categoryName: cat.active ? cat.name : `${cat.name} (deactivated)`,
                        amount: target?.pinnedAmount ?? carried,
                        carried,
                        hasPinned: target?.pinnedAmount !== undefined,
                        active: cat.active,
                        ownCarried: target?.inherited === false,
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
        setEditScope(row.hasPinned ? "THIS_MONTH" : "FORWARD");
    };

    const cancelEdit = () => {
        setEditingId(null);
        setEditAmount("");
        setEditExplanation("");
    };

    // A dense month (the current, grace, or a locked month) is written whole through /replace: each
    // row sends its carried amount, and the edited row also sends its pinned amount.
    const replaceWith = (changed: BudgetRow, carried: number, pinnedAmount: number | null) =>
        rows.map((r) =>
            r.categoryId === changed.categoryId
                ? { categoryId: r.categoryId, carriedAmount: carried, pinnedAmount }
                : { categoryId: r.categoryId, carriedAmount: r.carried },
        );

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
        const forward = editScope === "FORWARD";

        showLoading("Saving...");
        try {
            if (isFutureMonth) {
                // "From this month on" sets the carried amount and clears a pinned amount.
                // "This month only" sets only the pinned amount.
                const result = await updateBudget(
                    month,
                    [
                        forward
                            ? { categoryId: row.categoryId, carriedAmount: newAmount, pinnedAmount: null }
                            : { categoryId: row.categoryId, pinnedAmount: newAmount },
                    ],
                    explanation,
                );
                cancelEdit();
                await load();

                const { laterCarriedChanges, matchesInheritedValue } = result.warnings;
                const laterForThisCat = forward
                    ? laterCarriedChanges.filter((p) => p.categoryId === row.categoryId)
                    : [];
                if (laterForThisCat.length > 0) {
                    const monthsList = laterForThisCat.map((p) => p.yearMonth).join(", ");
                    const shouldClear = window.confirm(
                        `This category has changes in later months that won't see your new value: ${monthsList}. ` +
                            `Clear those later changes so the new value carries forward?`,
                    );
                    if (shouldClear) {
                        for (const later of laterForThisCat) {
                            await updateBudget(
                                later.yearMonth,
                                [{ categoryId: later.categoryId, carriedAmount: null }],
                                `Clearing later changes after a change of ${row.categoryName} in ${month}`,
                            );
                        }
                        await load();
                        showSuccess("Saved; later changes cleared");
                    } else {
                        showSuccess("Saved; later changes kept");
                    }
                } else if (forward && matchesInheritedValue.includes(row.categoryId)) {
                    showSuccess("Saved — note: this matches the value that would have carried forward anyway.");
                } else {
                    showSuccess("Saved");
                }
            } else {
                const targets = forward ? replaceWith(row, newAmount, null) : replaceWith(row, row.carried, newAmount);
                await replaceBudget(month, targets, explanation, willOverride);
                cancelEdit();
                await load();
                showSuccess(willOverride ? "Saved with override" : "Saved");
            }
            setTimeout(clear, 4000);
        } catch (err) {
            showError(err);
        }
    };

    // Undo the change of this month: a pinned amount first, else the own carried amount of a future
    // month. The month then shows the value carried from earlier months again.
    const handleUndo = async (row: BudgetRow) => {
        const what = row.hasPinned ? "the this-month-only change" : "the change in this month";
        if (!window.confirm(`Undo ${what} for ${row.categoryName}?`)) return;
        const explanation = `Undo ${what} for ${row.categoryName}`;
        showLoading("Undoing...");
        try {
            if (isFutureMonth) {
                await updateBudget(
                    month,
                    [
                        row.hasPinned
                            ? { categoryId: row.categoryId, pinnedAmount: null }
                            : { categoryId: row.categoryId, carriedAmount: null },
                    ],
                    explanation,
                );
            } else {
                await replaceBudget(month, replaceWith(row, row.carried, null), explanation, willOverride);
            }
            await load();
            showSuccess("Change undone");
            setTimeout(clear, 3000);
        } catch (err) {
            showError(err);
        }
    };

    const saveLabel = willOverride ? "Save with override" : "Save";

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
                    Future month: amounts carry forward from earlier months. A change applies from this month on, or to
                    this month only.
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
                                                {row.ownCarried && (
                                                    <span className="badge badge-pinned">changes here</span>
                                                )}
                                                {row.hasPinned && (
                                                    <span className="badge badge-pinned">this month only</span>
                                                )}
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
                                                        <ScopeChoice value={editScope} onChange={setEditScope} />
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
                                                            Edit
                                                        </button>
                                                        {(row.hasPinned || (isFutureMonth && row.ownCarried)) && (
                                                            <button
                                                                className="small-btn"
                                                                onClick={() => handleUndo(row)}
                                                            >
                                                                Undo change
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
