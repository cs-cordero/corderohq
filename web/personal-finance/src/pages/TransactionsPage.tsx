import { useCallback, useEffect, useMemo, useState } from "react";
import { useAdmin } from "../admin";
import {
    createTransaction,
    deleteTransactions,
    fetchAllCategories,
    fetchCategories,
    fetchTransactions,
    updateTransaction,
} from "../api";
import LoadingOverlay from "../components/LoadingOverlay";
import MonthPicker from "../components/MonthPicker";
import StatusMessage from "../components/StatusMessage";
import { editability } from "../editability";
import { currentYearMonth, formatCurrency, parseCurrencyInput } from "../format";
import { useStatus } from "../hooks";
import { DISPOSITION_LABELS, DISPOSITIONS, type Category, type Disposition, type Transaction } from "../types";

type SortKey = "transactionDate" | "description" | "amount" | "categoryId";
type SortDir = "asc" | "desc";

const EMPTY_EDIT = { description: "", amount: "", categoryId: "", disposition: "BUDGET" as Disposition, note: "" };
const EMPTY_NEW = { ...EMPTY_EDIT, date: "" };

export default function TransactionsPage() {
    const [month, setMonth] = useState(currentYearMonth);
    const [transactions, setTransactions] = useState<Transaction[]>([]);
    const [allCategories, setAllCategories] = useState<Category[]>([]);
    const [activeCategories, setActiveCategories] = useState<Category[]>([]);
    const { status, showLoading, showError, showSuccess, clear } = useStatus();
    const { adminModeOn } = useAdmin();

    const editState = editability(month);
    const isLocked = editState === "LOCKED";
    const isReadOnly = isLocked && !adminModeOn;
    const willOverride = isLocked && adminModeOn;
    const [showAddForm, setShowAddForm] = useState(false);
    const [editingId, setEditingId] = useState<string | null>(null);
    const [editValues, setEditValues] = useState(EMPTY_EDIT);
    const [newTxn, setNewTxn] = useState(EMPTY_NEW);

    // Multi-select
    const [selected, setSelected] = useState<Set<string>>(new Set());
    const [deleting, setDeleting] = useState(false);

    // Sorting
    const [sortKey, setSortKey] = useState<SortKey>("transactionDate");
    const [sortDir, setSortDir] = useState<SortDir>("asc");

    // Filters
    const [filterDescription, setFilterDescription] = useState("");
    const [filterCategory, setFilterCategory] = useState("");
    // The ledger hides SKIP rows by default.
    const [showSkipped, setShowSkipped] = useState(false);

    const load = useCallback(async () => {
        showLoading("Loading...");
        try {
            const [txns, cats, activeCats] = await Promise.all([
                fetchTransactions(month),
                fetchAllCategories(),
                fetchCategories(),
            ]);
            setTransactions(txns);
            setAllCategories(cats);
            setActiveCategories(activeCats);
            setSelected(new Set());
            clear();
        } catch (err) {
            showError(err);
        }
    }, [month]); // eslint-disable-line react-hooks/exhaustive-deps

    useEffect(() => {
        load();
    }, [load]);

    const categoryName = useCallback(
        (id: string | undefined) => {
            if (!id) return "";
            const cat = allCategories.find((c) => c.categoryId === id);
            return cat ? cat.name : id;
        },
        [allCategories],
    );

    const categoryLabel = useCallback(
        (id: string | undefined) => {
            if (!id) return "\u2014";
            const cat = allCategories.find((c) => c.categoryId === id);
            if (!cat) return id;
            return cat.active ? cat.name : `${cat.name} (deactivated)`;
        },
        [allCategories],
    );

    // Filter and sort
    const filteredAndSorted = useMemo(() => {
        let result = showSkipped ? transactions : transactions.filter((t) => t.disposition !== "SKIP");

        if (filterDescription) {
            const lower = filterDescription.toLowerCase();
            result = result.filter((t) => t.description.toLowerCase().includes(lower));
        }
        if (filterCategory) {
            result = result.filter((t) => t.categoryId === filterCategory);
        }

        result = [...result].sort((a, b) => {
            let cmp = 0;
            switch (sortKey) {
                case "transactionDate":
                    cmp = a.transactionDate.localeCompare(b.transactionDate);
                    break;
                case "description":
                    cmp = a.description.localeCompare(b.description);
                    break;
                case "amount":
                    cmp = a.amount - b.amount;
                    break;
                case "categoryId":
                    cmp = categoryName(a.categoryId).localeCompare(categoryName(b.categoryId));
                    break;
            }
            return sortDir === "asc" ? cmp : -cmp;
        });

        return result;
    }, [transactions, showSkipped, filterDescription, filterCategory, sortKey, sortDir, categoryName]);

    const handleSort = (key: SortKey) => {
        if (sortKey === key) {
            setSortDir(sortDir === "asc" ? "desc" : "asc");
        } else {
            setSortKey(key);
            setSortDir("asc");
        }
    };

    const sortIndicator = (key: SortKey) => {
        if (sortKey !== key) return " \u2195";
        return sortDir === "asc" ? " \u2191" : " \u2193";
    };

    // Selection
    const toggleSelect = (sortId: string) => {
        setSelected((prev) => {
            const next = new Set(prev);
            if (next.has(sortId)) next.delete(sortId);
            else next.add(sortId);
            return next;
        });
    };

    const toggleSelectAll = () => {
        if (selected.size === filteredAndSorted.length) {
            setSelected(new Set());
        } else {
            setSelected(new Set(filteredAndSorted.map((t) => t.sortId)));
        }
    };

    const handleBulkDelete = async () => {
        if (selected.size === 0) return;
        const confirmMsg = willOverride
            ? `Delete ${selected.size} transaction${selected.size > 1 ? "s" : ""} with admin override?`
            : `Delete ${selected.size} transaction${selected.size > 1 ? "s" : ""}?`;
        if (!confirm(confirmMsg)) return;
        setDeleting(true);
        try {
            const toDelete = transactions.filter((t) => selected.has(t.sortId));
            const items = toDelete.map((t) => ({ yearMonth: t.yearMonth, sortId: t.sortId }));
            const result = await deleteTransactions(items, willOverride);
            showSuccess(`Deleted ${result.deleted} transaction${result.deleted !== 1 ? "s" : ""}`);
            load();
        } catch (err) {
            showError(err);
        } finally {
            setDeleting(false);
        }
    };

    const handleAdd = async () => {
        const amount = parseCurrencyInput(newTxn.amount);
        const needsCategory = newTxn.disposition === "BUDGET";
        if (!newTxn.date || !newTxn.description || amount === null || (needsCategory && !newTxn.categoryId)) {
            showError(
                needsCategory
                    ? "Date, description, amount, and category are required"
                    : "Date, description, and amount are required",
            );
            return;
        }
        try {
            await createTransaction(
                {
                    yearMonth: month,
                    transactionDate: newTxn.date,
                    description: newTxn.description,
                    amount,
                    disposition: newTxn.disposition,
                    ...(newTxn.disposition !== "SKIP" && newTxn.categoryId ? { categoryId: newTxn.categoryId } : {}),
                    ...(newTxn.note.trim() ? { note: newTxn.note } : {}),
                },
                willOverride,
            );
            setNewTxn(EMPTY_NEW);
            setShowAddForm(false);
            load();
        } catch (err) {
            showError(err);
        }
    };

    const startEdit = (txn: Transaction) => {
        setEditingId(txn.sortId);
        setEditValues({
            description: txn.description,
            amount: (txn.amount / 1_000_000).toFixed(2),
            categoryId: txn.categoryId ?? "",
            disposition: txn.disposition,
            note: txn.note ?? "",
        });
    };

    const handleSaveEdit = async (txn: Transaction) => {
        const amount = parseCurrencyInput(editValues.amount);
        if (amount === null) return;
        if (editValues.disposition === "BUDGET" && !editValues.categoryId) {
            showError("A Budget transaction needs a category");
            return;
        }
        try {
            await updateTransaction(
                txn.yearMonth,
                txn.sortId,
                {
                    description: editValues.description,
                    amount,
                    disposition: editValues.disposition,
                    // An empty string removes the category or the note on the server.
                    categoryId: editValues.disposition === "SKIP" ? "" : editValues.categoryId,
                    note: editValues.note,
                },
                willOverride,
            );
            setEditingId(null);
            load();
        } catch (err) {
            showError(err);
        }
    };

    const handleDelete = async (txn: Transaction) => {
        const confirmMsg = willOverride
            ? `Delete "${txn.description}" with admin override?`
            : `Delete "${txn.description}"?`;
        if (!confirm(confirmMsg)) return;
        try {
            await deleteTransactions([{ yearMonth: txn.yearMonth, sortId: txn.sortId }], willOverride);
            load();
        } catch (err) {
            showError(err);
        }
    };

    // Unique categories present in current transactions (for filter dropdown)
    const categoriesInView = useMemo(() => {
        const ids = new Set(transactions.flatMap((t) => (t.categoryId ? [t.categoryId] : [])));
        return allCategories.filter((c) => ids.has(c.categoryId)).sort((a, b) => a.name.localeCompare(b.name));
    }, [transactions, allCategories]);

    return (
        <div className="page">
            <h1>Transactions</h1>
            <LoadingOverlay message={status.message} visible={status.type === "loading"} />
            <MonthPicker value={month} onChange={setMonth} />
            <StatusMessage message={status.type !== "loading" ? status.message : ""} type={status.type} />

            {isReadOnly && <p className="hint">This month is locked. Toggle Admin mode to edit with override.</p>}

            <div className="toolbar">
                <button className="primary-btn" onClick={() => setShowAddForm(!showAddForm)} disabled={isReadOnly}>
                    {showAddForm ? "Cancel" : "+ Add Transaction"}
                </button>
                {selected.size > 0 && (
                    <button
                        className="small-btn delete-btn"
                        onClick={handleBulkDelete}
                        disabled={deleting || isReadOnly}
                    >
                        {deleting
                            ? "Deleting..."
                            : willOverride
                              ? `Delete ${selected.size} (override)`
                              : `Delete ${selected.size} selected`}
                    </button>
                )}
            </div>

            {showAddForm && (
                <div className="add-form">
                    <input
                        type="date"
                        value={newTxn.date}
                        onChange={(e) => setNewTxn({ ...newTxn, date: e.target.value })}
                    />
                    <input
                        type="text"
                        placeholder="Description"
                        value={newTxn.description}
                        onChange={(e) => setNewTxn({ ...newTxn, description: e.target.value })}
                    />
                    <input
                        type="text"
                        placeholder="Amount"
                        className="currency-input"
                        value={newTxn.amount}
                        onChange={(e) => setNewTxn({ ...newTxn, amount: e.target.value })}
                    />
                    <select
                        value={newTxn.disposition}
                        onChange={(e) => setNewTxn({ ...newTxn, disposition: e.target.value as Disposition })}
                    >
                        {DISPOSITIONS.map((d) => (
                            <option key={d} value={d}>
                                {DISPOSITION_LABELS[d]}
                            </option>
                        ))}
                    </select>
                    {newTxn.disposition !== "SKIP" && (
                        <select
                            value={newTxn.categoryId}
                            onChange={(e) => setNewTxn({ ...newTxn, categoryId: e.target.value })}
                        >
                            <option value="">
                                {newTxn.disposition === "BUDGET" ? "Select category..." : "No category"}
                            </option>
                            {activeCategories.map((c) => (
                                <option key={c.categoryId} value={c.categoryId}>
                                    {c.name}
                                </option>
                            ))}
                        </select>
                    )}
                    <input
                        type="text"
                        placeholder="Note (optional)"
                        maxLength={200}
                        value={newTxn.note}
                        onChange={(e) => setNewTxn({ ...newTxn, note: e.target.value })}
                    />
                    <button className="primary-btn" onClick={handleAdd}>
                        {willOverride ? "Add (override)" : "Add"}
                    </button>
                </div>
            )}

            <div className="table-filters">
                <input
                    type="text"
                    placeholder="Filter by description..."
                    value={filterDescription}
                    onChange={(e) => setFilterDescription(e.target.value)}
                />
                <select value={filterCategory} onChange={(e) => setFilterCategory(e.target.value)}>
                    <option value="">All categories</option>
                    {categoriesInView.map((c) => (
                        <option key={c.categoryId} value={c.categoryId}>
                            {c.active ? c.name : `${c.name} (deactivated)`}
                        </option>
                    ))}
                </select>
                <label>
                    <input type="checkbox" checked={showSkipped} onChange={(e) => setShowSkipped(e.target.checked)} />{" "}
                    Show skipped rows
                </label>
                {(filterDescription || filterCategory) && (
                    <button
                        className="small-btn"
                        onClick={() => {
                            setFilterDescription("");
                            setFilterCategory("");
                        }}
                    >
                        Clear filters
                    </button>
                )}
            </div>

            <table className="data-table">
                <thead>
                    <tr>
                        <th className="checkbox-col">
                            <input
                                type="checkbox"
                                checked={filteredAndSorted.length > 0 && selected.size === filteredAndSorted.length}
                                onChange={toggleSelectAll}
                            />
                        </th>
                        <th className="sortable" onClick={() => handleSort("transactionDate")}>
                            Date{sortIndicator("transactionDate")}
                        </th>
                        <th className="sortable" onClick={() => handleSort("description")}>
                            Description{sortIndicator("description")}
                        </th>
                        <th className="sortable num" onClick={() => handleSort("amount")}>
                            Amount{sortIndicator("amount")}
                        </th>
                        <th className="sortable" onClick={() => handleSort("categoryId")}>
                            Category{sortIndicator("categoryId")}
                        </th>
                        <th>Type</th>
                        <th>Note</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {filteredAndSorted.map((txn) => (
                        <tr key={txn.sortId} className={selected.has(txn.sortId) ? "selected-row" : ""}>
                            <td className="checkbox-col">
                                <input
                                    type="checkbox"
                                    checked={selected.has(txn.sortId)}
                                    onChange={() => toggleSelect(txn.sortId)}
                                />
                            </td>
                            <td>{txn.transactionDate}</td>
                            {editingId === txn.sortId ? (
                                <>
                                    <td>
                                        <input
                                            type="text"
                                            value={editValues.description}
                                            onChange={(e) =>
                                                setEditValues({ ...editValues, description: e.target.value })
                                            }
                                        />
                                    </td>
                                    <td className="num">
                                        <input
                                            type="text"
                                            className="currency-input"
                                            value={editValues.amount}
                                            onChange={(e) => setEditValues({ ...editValues, amount: e.target.value })}
                                        />
                                    </td>
                                    <td>
                                        {editValues.disposition !== "SKIP" && (
                                            <select
                                                value={editValues.categoryId}
                                                onChange={(e) =>
                                                    setEditValues({ ...editValues, categoryId: e.target.value })
                                                }
                                            >
                                                <option value="">
                                                    {editValues.disposition === "BUDGET"
                                                        ? "Select category..."
                                                        : "No category"}
                                                </option>
                                                {txn.categoryId &&
                                                    !activeCategories.some((c) => c.categoryId === txn.categoryId) && (
                                                        <option value={txn.categoryId}>
                                                            {categoryLabel(txn.categoryId)}
                                                        </option>
                                                    )}
                                                {activeCategories.map((c) => (
                                                    <option key={c.categoryId} value={c.categoryId}>
                                                        {c.name}
                                                    </option>
                                                ))}
                                            </select>
                                        )}
                                    </td>
                                    <td>
                                        <select
                                            value={editValues.disposition}
                                            onChange={(e) =>
                                                setEditValues({
                                                    ...editValues,
                                                    disposition: e.target.value as Disposition,
                                                })
                                            }
                                        >
                                            {DISPOSITIONS.map((d) => (
                                                <option key={d} value={d}>
                                                    {DISPOSITION_LABELS[d]}
                                                </option>
                                            ))}
                                        </select>
                                    </td>
                                    <td>
                                        <input
                                            type="text"
                                            maxLength={200}
                                            value={editValues.note}
                                            onChange={(e) => setEditValues({ ...editValues, note: e.target.value })}
                                        />
                                    </td>
                                    <td className="actions">
                                        <button className="small-btn" onClick={() => handleSaveEdit(txn)}>
                                            {willOverride ? "Save (override)" : "Save"}
                                        </button>
                                        <button className="small-btn" onClick={() => setEditingId(null)}>
                                            Cancel
                                        </button>
                                    </td>
                                </>
                            ) : (
                                <>
                                    <td>{txn.description}</td>
                                    <td className={`num ${txn.amount < 0 ? "amount-refund" : ""}`}>
                                        {formatCurrency(txn.amount)}
                                        {txn.amount < 0 && <span className="badge badge-refund">refund</span>}
                                    </td>
                                    <td>{categoryLabel(txn.categoryId)}</td>
                                    <td>
                                        {txn.disposition !== "BUDGET" && (
                                            <span className={`badge badge-disposition-${txn.disposition}`}>
                                                {DISPOSITION_LABELS[txn.disposition]}
                                            </span>
                                        )}
                                    </td>
                                    <td>{txn.note ?? ""}</td>
                                    <td className="actions">
                                        <button
                                            className="small-btn"
                                            onClick={() => startEdit(txn)}
                                            disabled={isReadOnly}
                                        >
                                            Edit
                                        </button>
                                        <button
                                            className="small-btn delete-btn"
                                            onClick={() => handleDelete(txn)}
                                            disabled={isReadOnly}
                                        >
                                            {willOverride ? "Delete (override)" : "Delete"}
                                        </button>
                                    </td>
                                </>
                            )}
                        </tr>
                    ))}
                    {filteredAndSorted.length === 0 && (
                        <tr>
                            <td colSpan={8} className="empty">
                                {transactions.length === 0
                                    ? "No transactions for this month"
                                    : "No transactions match filters"}
                            </td>
                        </tr>
                    )}
                </tbody>
            </table>
        </div>
    );
}
