import { useCallback, useEffect, useMemo, useState } from "react";
import { useAdmin } from "../admin";
import {
    ApiError,
    createIncomeSource,
    createOneTimeLine,
    deactivateIncomeSource,
    deleteOneTimeLine,
    fetchAllIncomeSources,
    fetchIncomeMonth,
    fetchProfile,
    pinIncome,
    reactivateIncomeSource,
    replaceIncome,
    updateIncomeSource,
    updateOneTimeLine,
} from "../api";
import LoadingOverlay from "../components/LoadingOverlay";
import MonthPicker from "../components/MonthPicker";
import StatusMessage from "../components/StatusMessage";
import { editability } from "../editability";
import { currentYearMonth, formatCurrency, parseCurrencyInput } from "../format";
import { useStatus } from "../hooks";
import {
    DEDUCTION_KIND_LABELS,
    DEDUCTION_KINDS,
    PAY_FREQUENCIES,
    PAY_FREQUENCY_LABELS,
    type DeductionKind,
    type IncomeMonth,
    type IncomeMonthPaycheck,
    type IncomeSource,
    type OneTimeLine,
    type Paycheck,
    type Person,
} from "../types";

const MAX_EXPLANATION_LENGTH = 1000;

// --- The paycheck form -------------------------------------------------------
//
// The form holds each amount as the text that the user types. toPaycheck() turns it into
// millionths, and returns an error message when a value is not a number.

interface DeductionDraft {
    label: string;
    kind: DeductionKind;
    amount: string;
}

interface PaycheckDraft {
    paychecksPerMonth: string;
    gross: string;
    deductions: DeductionDraft[];
}

const EMPTY_PAYCHECK: PaycheckDraft = { paychecksPerMonth: "2", gross: "", deductions: [] };

function toDraft(paycheck: Paycheck): PaycheckDraft {
    return {
        paychecksPerMonth: String(paycheck.paychecksPerMonth),
        gross: (paycheck.gross / 1_000_000).toFixed(2),
        deductions: paycheck.deductions.map((d) => ({
            label: d.label,
            kind: d.kind,
            amount: (d.amount / 1_000_000).toFixed(2),
        })),
    };
}

function toPaycheck(draft: PaycheckDraft): Paycheck | string {
    const paychecksPerMonth = parseInt(draft.paychecksPerMonth, 10);
    if (!Number.isInteger(paychecksPerMonth) || paychecksPerMonth < 1 || paychecksPerMonth > 5) {
        return "Paychecks per month must be from 1 to 5";
    }
    const gross = parseCurrencyInput(draft.gross);
    if (gross === null) return "Gross pay must be an amount";
    const deductions: Paycheck["deductions"] = [];
    for (const d of draft.deductions) {
        const amount = parseCurrencyInput(d.amount);
        if (amount === null) return `The amount of "${d.label || "a deduction"}" must be an amount`;
        if (!d.label.trim()) return "Each deduction needs a label";
        deductions.push({ label: d.label.trim(), kind: d.kind, amount });
    }
    return { paychecksPerMonth, gross, deductions };
}

function draftNet(draft: PaycheckDraft): number | null {
    const paycheck = toPaycheck(draft);
    if (typeof paycheck === "string") return null;
    return paycheck.gross - paycheck.deductions.reduce((sum, d) => sum + d.amount, 0);
}

function PaycheckForm({ draft, onChange }: { draft: PaycheckDraft; onChange: (draft: PaycheckDraft) => void }) {
    const net = draftNet(draft);
    const setDeduction = (index: number, change: Partial<DeductionDraft>) =>
        onChange({
            ...draft,
            deductions: draft.deductions.map((d, i) => (i === index ? { ...d, ...change } : d)),
        });
    return (
        <div className="paycheck-form">
            <label>
                Gross pay (one paycheck){" "}
                <input
                    type="text"
                    className="currency-input"
                    value={draft.gross}
                    onChange={(e) => onChange({ ...draft, gross: e.target.value })}
                    placeholder="0.00"
                />
            </label>
            <table className="data-table">
                <thead>
                    <tr>
                        <th>Deduction</th>
                        <th>Kind</th>
                        <th className="num">Amount</th>
                        <th />
                    </tr>
                </thead>
                <tbody>
                    {draft.deductions.map((d, i) => (
                        <tr key={i}>
                            <td>
                                <input
                                    type="text"
                                    value={d.label}
                                    onChange={(e) => setDeduction(i, { label: e.target.value })}
                                    placeholder="Label"
                                />
                            </td>
                            <td>
                                <select
                                    value={d.kind}
                                    onChange={(e) => setDeduction(i, { kind: e.target.value as DeductionKind })}
                                >
                                    {DEDUCTION_KINDS.map((k) => (
                                        <option key={k} value={k}>
                                            {DEDUCTION_KIND_LABELS[k]}
                                        </option>
                                    ))}
                                </select>
                            </td>
                            <td className="num">
                                <input
                                    type="text"
                                    className="currency-input"
                                    value={d.amount}
                                    onChange={(e) => setDeduction(i, { amount: e.target.value })}
                                    placeholder="0.00"
                                />
                            </td>
                            <td>
                                <button
                                    className="small-btn"
                                    onClick={() =>
                                        onChange({ ...draft, deductions: draft.deductions.filter((_, j) => j !== i) })
                                    }
                                >
                                    Remove
                                </button>
                            </td>
                        </tr>
                    ))}
                </tbody>
            </table>
            <button
                className="small-btn"
                onClick={() =>
                    onChange({
                        ...draft,
                        deductions: [...draft.deductions, { label: "", kind: "TAX", amount: "" }],
                    })
                }
            >
                + Add deduction
            </button>
            <p>
                Net pay (one paycheck): <strong>{net === null ? "—" : formatCurrency(net)}</strong>
            </p>
            <label>
                Paychecks per month in the plan{" "}
                <input
                    type="number"
                    min={1}
                    max={5}
                    value={draft.paychecksPerMonth}
                    onChange={(e) => onChange({ ...draft, paychecksPerMonth: e.target.value })}
                />
            </label>
        </div>
    );
}

function frequencyLabel(frequency: string): string {
    const match = PAY_FREQUENCIES.find((f) => f === frequency);
    return match ? PAY_FREQUENCY_LABELS[match] : frequency;
}

// --- The read-only breakdown --------------------------------------------------

function PaycheckBreakdown({ paycheck }: { paycheck: IncomeMonthPaycheck }) {
    return (
        <table className="data-table paycheck-breakdown">
            <tbody>
                <tr>
                    <td>Gross pay</td>
                    <td />
                    <td className="num">{formatCurrency(paycheck.gross)}</td>
                </tr>
                {paycheck.deductions.map((d, i) => (
                    <tr key={i}>
                        <td>{d.label}</td>
                        <td className="muted">{DEDUCTION_KIND_LABELS[d.kind]}</td>
                        <td className="num">−{formatCurrency(d.amount)}</td>
                    </tr>
                ))}
                <tr>
                    <td>
                        <strong>Net pay</strong>
                    </td>
                    <td />
                    <td className="num">
                        <strong>{formatCurrency(paycheck.net)}</strong>
                    </td>
                </tr>
                <tr>
                    <td>
                        × {paycheck.paychecksPerMonth} paycheck{paycheck.paychecksPerMonth !== 1 ? "s" : ""} in
                        the plan
                    </td>
                    <td />
                    <td className="num">
                        <strong>{formatCurrency(paycheck.monthlyNet)}</strong>
                    </td>
                </tr>
            </tbody>
        </table>
    );
}

// --- The page -----------------------------------------------------------------

interface NewSourceDraft {
    personId: string;
    label: string;
    frequency: string;
    paycheck: PaycheckDraft;
    explanation: string;
}

const EMPTY_NEW_SOURCE: NewSourceDraft = {
    personId: "",
    label: "",
    frequency: "biweekly",
    paycheck: EMPTY_PAYCHECK,
    explanation: "",
};

interface OneTimeDraft {
    lineId: string | null; // null for a new line
    label: string;
    amount: string;
    explanation: string;
}

export default function IncomePage() {
    const [month, setMonth] = useState(currentYearMonth);
    const [income, setIncome] = useState<IncomeMonth | null>(null);
    const [sources, setSources] = useState<IncomeSource[]>([]);
    const [people, setPeople] = useState<Person[]>([]);
    const { status, showLoading, showSuccess, showError, clear } = useStatus();
    const { adminModeOn } = useAdmin();

    // The paycheck that is open for edit, by incomeSourceId.
    const [editingId, setEditingId] = useState<string | null>(null);
    const [editDraft, setEditDraft] = useState<PaycheckDraft>(EMPTY_PAYCHECK);
    const [editExplanation, setEditExplanation] = useState("");

    const [oneTimeDraft, setOneTimeDraft] = useState<OneTimeDraft | null>(null);
    const [newSource, setNewSource] = useState<NewSourceDraft | null>(null);
    const [renaming, setRenaming] = useState<{ id: string; label: string } | null>(null);
    const [pendingDeactivate, setPendingDeactivate] = useState<{
        source: IncomeSource;
        affectedMonths: string[];
        explanation: string;
    } | null>(null);

    const editState = editability(month);
    const isLocked = editState === "LOCKED";
    const isReadOnly = isLocked && !adminModeOn;
    const willOverride = isLocked && adminModeOn;
    // A future month is sparse, the same as the budget: an edit there makes a pin.
    const isFutureMonth = month > currentYearMonth();

    const load = useCallback(async () => {
        showLoading("Loading...");
        try {
            const [monthData, allSources, allPeople] = await Promise.all([
                fetchIncomeMonth(month),
                fetchAllIncomeSources(),
                fetchProfile(),
            ]);
            setIncome(monthData);
            setSources(allSources);
            setPeople(allPeople);
            setEditingId(null);
            setOneTimeDraft(null);
            clear();
        } catch (err) {
            showError(err);
        }
    }, [month]); // eslint-disable-line react-hooks/exhaustive-deps

    useEffect(() => {
        load();
    }, [load]);

    const personName = useCallback(
        (personId: string) => people.find((p) => p.personId === personId)?.name ?? "Unknown person",
        [people],
    );

    // An active source with no paycheck row in a dense month, such as a reactivated source.
    // The user can give it a paycheck in this month.
    const sourcesWithoutPaycheck = useMemo(() => {
        if (!income) return [];
        const withPaycheck = new Set(income.paychecks.map((p) => p.incomeSourceId));
        return sources.filter((s) => s.active && !withPaycheck.has(s.incomeSourceId));
    }, [income, sources]);

    const startEdit = (incomeSourceId: string, paycheck: Paycheck | null) => {
        setEditingId(incomeSourceId);
        setEditDraft(paycheck ? toDraft(paycheck) : EMPTY_PAYCHECK);
        setEditExplanation("");
    };

    const cancelEdit = () => {
        setEditingId(null);
        setEditExplanation("");
    };

    const handleSavePaycheck = async (incomeSourceId: string, label: string) => {
        if (!income) return;
        const paycheck = toPaycheck(editDraft);
        if (typeof paycheck === "string") {
            showError(paycheck);
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
                const result = await pinIncome(month, [{ incomeSourceId, paycheck }], explanation);
                setIncome(result.income);
                cancelEdit();
                const downstream = result.warnings.downstreamPins.filter((p) => p.incomeSourceId === incomeSourceId);
                if (downstream.length > 0) {
                    const monthsList = downstream.map((p) => p.yearMonth).join(", ");
                    const shouldClear = window.confirm(
                        `${label} has pins in later months that won't see this paycheck: ${monthsList}. ` +
                            `Clear those pins so this paycheck carries forward?`,
                    );
                    if (shouldClear) {
                        for (const pin of downstream) {
                            await pinIncome(
                                pin.yearMonth,
                                [{ incomeSourceId, paycheck: null }],
                                `Clearing later pins after a pin of ${label} in ${month}`,
                            );
                        }
                        await load();
                        showSuccess("Pin saved; later pins cleared");
                    } else {
                        showSuccess("Pin saved; later pins kept");
                    }
                } else if (result.warnings.pinMatchesCarriedValue.includes(incomeSourceId)) {
                    showSuccess(
                        "Pin saved — note: this matches the paycheck that would carry forward without a pin.",
                    );
                } else {
                    showSuccess("Pin saved");
                }
            } else {
                // /replace needs every source with a row in this month. Send the others as they are.
                const others = income.paychecks
                    .filter((p) => p.incomeSourceId !== incomeSourceId)
                    .map((p) => ({
                        incomeSourceId: p.incomeSourceId,
                        paycheck: { paychecksPerMonth: p.paychecksPerMonth, gross: p.gross, deductions: p.deductions },
                    }));
                const updated = await replaceIncome(
                    month,
                    [...others, { incomeSourceId, paycheck }],
                    explanation,
                    willOverride,
                );
                setIncome(updated);
                cancelEdit();
                showSuccess(willOverride ? "Saved with override" : "Saved");
            }
            setTimeout(clear, 4000);
        } catch (err) {
            showError(err);
        }
    };

    const handleRemovePin = async (paycheck: IncomeMonthPaycheck) => {
        const message = `Remove the pin for ${paycheck.label}? The paycheck will carry forward from earlier months.`;
        if (!window.confirm(message)) {
            return;
        }
        showLoading("Removing pin...");
        try {
            await pinIncome(
                month,
                [{ incomeSourceId: paycheck.incomeSourceId, paycheck: null }],
                `Removed pin for ${paycheck.label}`,
            );
            await load();
            showSuccess("Pin removed");
            setTimeout(clear, 3000);
        } catch (err) {
            showError(err);
        }
    };

    const handleSaveOneTime = async () => {
        if (!oneTimeDraft) return;
        const amount = parseCurrencyInput(oneTimeDraft.amount);
        if (!oneTimeDraft.label.trim() || amount === null || amount <= 0) {
            showError("A one-time line needs a label and an amount more than 0");
            return;
        }
        const explanation = oneTimeDraft.explanation.trim();
        if (!explanation) {
            showError("Explanation is required");
            return;
        }
        showLoading("Saving...");
        try {
            const data = { label: oneTimeDraft.label.trim(), amount, explanation };
            if (oneTimeDraft.lineId === null) {
                await createOneTimeLine(month, data, willOverride);
            } else {
                await updateOneTimeLine(month, { ...data, lineId: oneTimeDraft.lineId }, willOverride);
            }
            await load();
            showSuccess("One-time line saved");
            setTimeout(clear, 3000);
        } catch (err) {
            showError(err);
        }
    };

    const handleDeleteOneTime = async (line: OneTimeLine) => {
        const explanation = window.prompt(`Delete "${line.label}"? Give an explanation:`);
        if (!explanation || !explanation.trim()) return;
        showLoading("Deleting...");
        try {
            await deleteOneTimeLine(month, { lineId: line.lineId, explanation: explanation.trim() }, willOverride);
            await load();
            showSuccess("One-time line deleted");
            setTimeout(clear, 3000);
        } catch (err) {
            showError(err);
        }
    };

    const handleCreateSource = async () => {
        if (!newSource) return;
        const paycheck = toPaycheck(newSource.paycheck);
        if (typeof paycheck === "string") {
            showError(paycheck);
            return;
        }
        if (!newSource.personId || !newSource.label.trim() || !newSource.explanation.trim()) {
            showError("Person, label, and explanation are required");
            return;
        }
        showLoading("Saving...");
        try {
            await createIncomeSource({
                personId: newSource.personId,
                label: newSource.label.trim(),
                frequency: newSource.frequency,
                initialPaycheck: paycheck,
                explanation: newSource.explanation.trim(),
            });
            setNewSource(null);
            await load();
            showSuccess("Income source added");
            setTimeout(clear, 3000);
        } catch (err) {
            showError(err);
        }
    };

    const handleRename = async () => {
        if (!renaming || !renaming.label.trim()) return;
        try {
            await updateIncomeSource(renaming.id, { label: renaming.label.trim() });
            setRenaming(null);
            await load();
        } catch (err) {
            showError(err);
        }
    };

    const handleDeactivate = async (source: IncomeSource) => {
        try {
            await deactivateIncomeSource(source.incomeSourceId);
            await load();
            showSuccess(`Deactivated ${source.label}`);
        } catch (err) {
            // A source with future pins needs a confirmation and an explanation first.
            if (err instanceof ApiError && err.status === 409) {
                try {
                    const body: unknown = JSON.parse(err.responseBody);
                    const months =
                        typeof body === "object" && body !== null && "affectedMonths" in body
                            ? (body as { affectedMonths: unknown }).affectedMonths
                            : null;
                    if (Array.isArray(months)) {
                        setPendingDeactivate({ source, affectedMonths: months.map(String), explanation: "" });
                        return;
                    }
                } catch {
                    // Fall through to the error message.
                }
            }
            showError(err);
        }
    };

    const handleConfirmDeactivate = async () => {
        if (!pendingDeactivate || !pendingDeactivate.explanation.trim()) {
            showError("Explanation is required");
            return;
        }
        try {
            await deactivateIncomeSource(pendingDeactivate.source.incomeSourceId, {
                confirm: true,
                explanation: pendingDeactivate.explanation.trim(),
            });
            setPendingDeactivate(null);
            await load();
            showSuccess("Income source deactivated; future pins dropped");
        } catch (err) {
            showError(err);
        }
    };

    const handleReactivate = async (source: IncomeSource) => {
        try {
            await reactivateIncomeSource(source.incomeSourceId);
            await load();
        } catch (err) {
            showError(err);
        }
    };

    const saveLabel = isFutureMonth ? "Pin" : willOverride ? "Save with override" : "Save";

    const renderEditor = (incomeSourceId: string, label: string) => (
        <div className="paycheck-editor">
            <PaycheckForm draft={editDraft} onChange={setEditDraft} />
            <div className="edit-actions">
                <input
                    type="text"
                    className="explanation-inline"
                    value={editExplanation}
                    onChange={(e) => setEditExplanation(e.target.value)}
                    placeholder="Explanation (required)"
                    maxLength={MAX_EXPLANATION_LENGTH}
                />
                <button className="small-btn" onClick={() => handleSavePaycheck(incomeSourceId, label)}>
                    {saveLabel}
                </button>
                <button className="small-btn" onClick={cancelEdit}>
                    Cancel
                </button>
            </div>
        </div>
    );

    return (
        <div className="page">
            <h1>Income</h1>
            <LoadingOverlay message={status.message} visible={status.type === "loading"} />
            <MonthPicker value={month} onChange={setMonth} />
            <StatusMessage message={status.type !== "loading" ? status.message : ""} type={status.type} />

            {isReadOnly && <p className="hint">This month is locked. Toggle Admin mode to edit with override.</p>}
            {isFutureMonth && (
                <p className="hint">Future month: paychecks carry forward from earlier months. Edits create pins.</p>
            )}

            {income && (
                <>
                    <h2>
                        Planned net income: <span className="num">{formatCurrency(income.plannedNetIncome)}</span>
                    </h2>

                    <h2>Paychecks</h2>
                    {income.paychecks.length === 0 && sourcesWithoutPaycheck.length === 0 && (
                        <p className="hint">No income sources yet. Add one below.</p>
                    )}
                    {income.paychecks.map((p) => (
                        <div key={p.incomeSourceId} className={`paycheck-card ${p.active ? "" : "faded"}`}>
                            <h3>
                                {p.label}
                                <span className="muted"> — {personName(p.personId)}</span>
                                {!p.active && <span className="muted"> (deactivated)</span>}
                                {p.pinned && <span className="badge badge-pinned">pinned</span>}
                            </h3>
                            {editingId === p.incomeSourceId ? (
                                renderEditor(p.incomeSourceId, p.label)
                            ) : (
                                <>
                                    <PaycheckBreakdown paycheck={p} />
                                    {!isReadOnly && p.active && (
                                        <div className="edit-actions">
                                            <button
                                                className="small-btn"
                                                onClick={() => startEdit(p.incomeSourceId, p)}
                                            >
                                                {isFutureMonth ? (p.pinned ? "Edit pin" : "Pin") : "Edit"}
                                            </button>
                                            {isFutureMonth && p.pinned && (
                                                <button className="small-btn" onClick={() => handleRemovePin(p)}>
                                                    Remove pin
                                                </button>
                                            )}
                                        </div>
                                    )}
                                </>
                            )}
                        </div>
                    ))}
                    {!isReadOnly &&
                        sourcesWithoutPaycheck.map((s) => (
                            <div key={s.incomeSourceId} className="paycheck-card">
                                <h3>
                                    {s.label}
                                    <span className="muted"> — {personName(s.personId)}</span>
                                </h3>
                                {editingId === s.incomeSourceId ? (
                                    renderEditor(s.incomeSourceId, s.label)
                                ) : (
                                    <>
                                        <p className="hint">No paycheck in this month.</p>
                                        <button className="small-btn" onClick={() => startEdit(s.incomeSourceId, null)}>
                                            Add paycheck
                                        </button>
                                    </>
                                )}
                            </div>
                        ))}

                    <h2>One-time income</h2>
                    <p className="hint">
                        A bonus, an RSU payout, a third paycheck, or other extra money that you want to budget. It
                        counts in this month only.
                    </p>
                    <table className="data-table">
                        <tbody>
                            {income.oneTime.map((line) => (
                                <tr key={line.lineId}>
                                    <td>{line.label}</td>
                                    <td className="num">{formatCurrency(line.amount)}</td>
                                    <td className="actions">
                                        {!isReadOnly && (
                                            <>
                                                <button
                                                    className="small-btn"
                                                    onClick={() =>
                                                        setOneTimeDraft({
                                                            lineId: line.lineId,
                                                            label: line.label,
                                                            amount: (line.amount / 1_000_000).toFixed(2),
                                                            explanation: "",
                                                        })
                                                    }
                                                >
                                                    Edit
                                                </button>
                                                <button
                                                    className="small-btn delete-btn"
                                                    onClick={() => handleDeleteOneTime(line)}
                                                >
                                                    Delete
                                                </button>
                                            </>
                                        )}
                                    </td>
                                </tr>
                            ))}
                            {income.oneTime.length === 0 && (
                                <tr>
                                    <td colSpan={3} className="empty">
                                        No one-time income in this month
                                    </td>
                                </tr>
                            )}
                        </tbody>
                    </table>
                    {!isReadOnly && oneTimeDraft === null && (
                        <button
                            className="small-btn"
                            onClick={() => setOneTimeDraft({ lineId: null, label: "", amount: "", explanation: "" })}
                        >
                            + Add one-time income
                        </button>
                    )}
                    {oneTimeDraft && (
                        <div className="add-form">
                            <input
                                type="text"
                                placeholder="Label"
                                value={oneTimeDraft.label}
                                onChange={(e) => setOneTimeDraft({ ...oneTimeDraft, label: e.target.value })}
                            />
                            <input
                                type="text"
                                className="currency-input"
                                placeholder="Net amount"
                                value={oneTimeDraft.amount}
                                onChange={(e) => setOneTimeDraft({ ...oneTimeDraft, amount: e.target.value })}
                            />
                            <input
                                type="text"
                                className="explanation-inline"
                                placeholder="Explanation (required)"
                                maxLength={MAX_EXPLANATION_LENGTH}
                                value={oneTimeDraft.explanation}
                                onChange={(e) => setOneTimeDraft({ ...oneTimeDraft, explanation: e.target.value })}
                            />
                            <button className="primary-btn" onClick={handleSaveOneTime}>
                                {willOverride ? "Save with override" : "Save"}
                            </button>
                            <button className="small-btn" onClick={() => setOneTimeDraft(null)}>
                                Cancel
                            </button>
                        </div>
                    )}
                </>
            )}

            <h2>Income sources</h2>
            <p className="hint">
                An income source is any stream of income that you plan, such as a job, interest, or dividends.
                A change here affects every month. A change to a paycheck goes in the month above.
            </p>
            <table className="data-table">
                <thead>
                    <tr>
                        <th>Label</th>
                        <th>Person</th>
                        <th>Pay frequency</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {sources.map((s) => (
                        <tr key={s.incomeSourceId} className={s.active ? "" : "faded"}>
                            <td>
                                {renaming?.id === s.incomeSourceId ? (
                                    <input
                                        type="text"
                                        value={renaming.label}
                                        onChange={(e) => setRenaming({ id: s.incomeSourceId, label: e.target.value })}
                                    />
                                ) : (
                                    <>
                                        {s.label}
                                        {!s.active && <span className="muted"> (deactivated)</span>}
                                    </>
                                )}
                            </td>
                            <td>{personName(s.personId)}</td>
                            <td>{frequencyLabel(s.frequency)}</td>
                            <td className="actions">
                                {renaming?.id === s.incomeSourceId ? (
                                    <>
                                        <button className="small-btn" onClick={handleRename}>
                                            Save
                                        </button>
                                        <button className="small-btn" onClick={() => setRenaming(null)}>
                                            Cancel
                                        </button>
                                    </>
                                ) : (
                                    <>
                                        <button
                                            className="small-btn"
                                            onClick={() => setRenaming({ id: s.incomeSourceId, label: s.label })}
                                        >
                                            Rename
                                        </button>
                                        {s.active ? (
                                            <button
                                                className="small-btn delete-btn"
                                                onClick={() => handleDeactivate(s)}
                                            >
                                                Deactivate
                                            </button>
                                        ) : (
                                            <button className="small-btn" onClick={() => handleReactivate(s)}>
                                                Reactivate
                                            </button>
                                        )}
                                    </>
                                )}
                            </td>
                        </tr>
                    ))}
                </tbody>
            </table>

            {pendingDeactivate && (
                <div className="add-form">
                    <p>
                        {pendingDeactivate.source.label} has pins in future months:{" "}
                        {pendingDeactivate.affectedMonths.join(", ")}. Deactivation drops them.
                    </p>
                    <input
                        type="text"
                        className="explanation-inline"
                        placeholder="Explanation (required)"
                        maxLength={MAX_EXPLANATION_LENGTH}
                        value={pendingDeactivate.explanation}
                        onChange={(e) => setPendingDeactivate({ ...pendingDeactivate, explanation: e.target.value })}
                    />
                    <button className="small-btn delete-btn" onClick={handleConfirmDeactivate}>
                        Deactivate and drop pins
                    </button>
                    <button className="small-btn" onClick={() => setPendingDeactivate(null)}>
                        Cancel
                    </button>
                </div>
            )}

            {newSource === null ? (
                <button className="primary-btn" onClick={() => setNewSource(EMPTY_NEW_SOURCE)}>
                    + Add income source
                </button>
            ) : (
                <div className="add-form paycheck-editor">
                    <select
                        value={newSource.personId}
                        onChange={(e) => setNewSource({ ...newSource, personId: e.target.value })}
                    >
                        <option value="">Select person...</option>
                        {people.map((p) => (
                            <option key={p.personId} value={p.personId}>
                                {p.name}
                            </option>
                        ))}
                    </select>
                    <input
                        type="text"
                        placeholder="Label, such as the employer"
                        value={newSource.label}
                        onChange={(e) => setNewSource({ ...newSource, label: e.target.value })}
                    />
                    <select
                        value={newSource.frequency}
                        onChange={(e) => setNewSource({ ...newSource, frequency: e.target.value })}
                    >
                        {PAY_FREQUENCIES.map((f) => (
                            <option key={f} value={f}>
                                {PAY_FREQUENCY_LABELS[f]}
                            </option>
                        ))}
                    </select>
                    <p className="hint">The first paycheck goes in the current month, and carries forward.</p>
                    <PaycheckForm
                        draft={newSource.paycheck}
                        onChange={(paycheck) => setNewSource({ ...newSource, paycheck })}
                    />
                    <input
                        type="text"
                        className="explanation-inline"
                        placeholder="Explanation (required)"
                        maxLength={MAX_EXPLANATION_LENGTH}
                        value={newSource.explanation}
                        onChange={(e) => setNewSource({ ...newSource, explanation: e.target.value })}
                    />
                    <button className="primary-btn" onClick={handleCreateSource}>
                        Add
                    </button>
                    <button className="small-btn" onClick={() => setNewSource(null)}>
                        Cancel
                    </button>
                </div>
            )}
        </div>
    );
}
