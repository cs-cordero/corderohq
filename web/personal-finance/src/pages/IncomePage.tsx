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
    type IncomeMonthPayment,
    type IncomeSource,
    type OneTimeLine,
    type Payment,
    type Person,
} from "../types";

const MAX_EXPLANATION_LENGTH = 1000;

// --- The payment form -------------------------------------------------------
//
// The form holds each amount as the text that the user types. toPayment() turns it into
// millionths, and returns an error message when a value is not a number.

interface DeductionDraft {
    label: string;
    kind: DeductionKind;
    amount: string;
}

interface PaymentDraft {
    paymentsPerMonth: string;
    gross: string;
    deductions: DeductionDraft[];
}

const EMPTY_PAYCHECK: PaymentDraft = { paymentsPerMonth: "2", gross: "", deductions: [] };

function toDraft(payment: Payment): PaymentDraft {
    return {
        paymentsPerMonth: String(payment.paymentsPerMonth),
        gross: (payment.gross / 1_000_000).toFixed(2),
        deductions: payment.deductions.map((d) => ({
            label: d.label,
            kind: d.kind,
            amount: (d.amount / 1_000_000).toFixed(2),
        })),
    };
}

function toPayment(draft: PaymentDraft): Payment | string {
    const paymentsPerMonth = parseInt(draft.paymentsPerMonth, 10);
    if (!Number.isInteger(paymentsPerMonth) || paymentsPerMonth < 1 || paymentsPerMonth > 5) {
        return "Payments per month must be from 1 to 5";
    }
    const gross = parseCurrencyInput(draft.gross);
    if (gross === null) return "Gross amount must be an amount";
    const deductions: Payment["deductions"] = [];
    for (const d of draft.deductions) {
        const amount = parseCurrencyInput(d.amount);
        if (amount === null) return `The amount of "${d.label || "a deduction"}" must be an amount`;
        if (!d.label.trim()) return "Each deduction needs a label";
        deductions.push({ label: d.label.trim(), kind: d.kind, amount });
    }
    return { paymentsPerMonth, gross, deductions };
}

function draftNet(draft: PaymentDraft): number | null {
    const payment = toPayment(draft);
    if (typeof payment === "string") return null;
    return payment.gross - payment.deductions.reduce((sum, d) => sum + d.amount, 0);
}

function PaymentForm({ draft, onChange }: { draft: PaymentDraft; onChange: (draft: PaymentDraft) => void }) {
    const net = draftNet(draft);
    const setDeduction = (index: number, change: Partial<DeductionDraft>) =>
        onChange({
            ...draft,
            deductions: draft.deductions.map((d, i) => (i === index ? { ...d, ...change } : d)),
        });
    return (
        <div className="payment-form">
            <label>
                Gross amount (one payment){" "}
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
                Net amount (one payment): <strong>{net === null ? "—" : formatCurrency(net)}</strong>
            </p>
            <label>
                Payments per month in the plan{" "}
                <input
                    type="number"
                    min={1}
                    max={5}
                    value={draft.paymentsPerMonth}
                    onChange={(e) => onChange({ ...draft, paymentsPerMonth: e.target.value })}
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

function PaymentBreakdown({ payment }: { payment: IncomeMonthPayment }) {
    return (
        <table className="data-table payment-breakdown">
            <tbody>
                <tr>
                    <td>Gross amount</td>
                    <td />
                    <td className="num">{formatCurrency(payment.gross)}</td>
                </tr>
                {payment.deductions.map((d, i) => (
                    <tr key={i}>
                        <td>{d.label}</td>
                        <td className="muted">{DEDUCTION_KIND_LABELS[d.kind]}</td>
                        <td className="num">−{formatCurrency(d.amount)}</td>
                    </tr>
                ))}
                <tr>
                    <td>
                        <strong>Net amount</strong>
                    </td>
                    <td />
                    <td className="num">
                        <strong>{formatCurrency(payment.net)}</strong>
                    </td>
                </tr>
                <tr>
                    <td>
                        × {payment.paymentsPerMonth} payment{payment.paymentsPerMonth !== 1 ? "s" : ""} in the plan
                    </td>
                    <td />
                    <td className="num">
                        <strong>{formatCurrency(payment.monthlyNet)}</strong>
                    </td>
                </tr>
            </tbody>
        </table>
    );
}

// --- The page -----------------------------------------------------------------

interface NewSourceDraft {
    owners: string[];
    label: string;
    frequency: string;
    payment: PaymentDraft;
    explanation: string;
}

const EMPTY_NEW_SOURCE: NewSourceDraft = {
    owners: [],
    label: "",
    frequency: "biweekly",
    payment: EMPTY_PAYCHECK,
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

    // The payment that is open for edit, by incomeSourceId.
    const [editingId, setEditingId] = useState<string | null>(null);
    const [editDraft, setEditDraft] = useState<PaymentDraft>(EMPTY_PAYCHECK);
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

    // The names of the owners of a source. A joint source has two or more.
    const ownerNames = useCallback(
        (owners: string[]) =>
            owners.map((id) => people.find((p) => p.personId === id)?.name ?? "Unknown person").join(" & "),
        [people],
    );

    // An active source with no payment row in a dense month, such as a reactivated source.
    // The user can give it a payment in this month.
    const sourcesWithoutPayment = useMemo(() => {
        if (!income) return [];
        const withPayment = new Set(income.payments.map((p) => p.incomeSourceId));
        return sources.filter((s) => s.active && !withPayment.has(s.incomeSourceId));
    }, [income, sources]);

    const startEdit = (incomeSourceId: string, payment: Payment | null) => {
        setEditingId(incomeSourceId);
        setEditDraft(payment ? toDraft(payment) : EMPTY_PAYCHECK);
        setEditExplanation("");
    };

    const cancelEdit = () => {
        setEditingId(null);
        setEditExplanation("");
    };

    const handleSavePayment = async (incomeSourceId: string, label: string) => {
        if (!income) return;
        const payment = toPayment(editDraft);
        if (typeof payment === "string") {
            showError(payment);
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
                const result = await pinIncome(month, [{ incomeSourceId, payment }], explanation);
                setIncome(result.income);
                cancelEdit();
                const downstream = result.warnings.downstreamPins.filter((p) => p.incomeSourceId === incomeSourceId);
                if (downstream.length > 0) {
                    const monthsList = downstream.map((p) => p.yearMonth).join(", ");
                    const shouldClear = window.confirm(
                        `${label} has pins in later months that won't see this payment: ${monthsList}. ` +
                            `Clear those pins so this payment carries forward?`,
                    );
                    if (shouldClear) {
                        for (const pin of downstream) {
                            await pinIncome(
                                pin.yearMonth,
                                [{ incomeSourceId, payment: null }],
                                `Clearing later pins after a pin of ${label} in ${month}`,
                            );
                        }
                        await load();
                        showSuccess("Pin saved; later pins cleared");
                    } else {
                        showSuccess("Pin saved; later pins kept");
                    }
                } else if (result.warnings.pinMatchesCarriedValue.includes(incomeSourceId)) {
                    showSuccess("Pin saved — note: this matches the payment that would carry forward without a pin.");
                } else {
                    showSuccess("Pin saved");
                }
            } else {
                // /replace needs every source with a row in this month. Send the others as they are.
                const others = income.payments
                    .filter((p) => p.incomeSourceId !== incomeSourceId)
                    .map((p) => ({
                        incomeSourceId: p.incomeSourceId,
                        payment: { paymentsPerMonth: p.paymentsPerMonth, gross: p.gross, deductions: p.deductions },
                    }));
                const updated = await replaceIncome(
                    month,
                    [...others, { incomeSourceId, payment }],
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

    const handleRemovePin = async (payment: IncomeMonthPayment) => {
        const message = `Remove the pin for ${payment.label}? The payment will carry forward from earlier months.`;
        if (!window.confirm(message)) {
            return;
        }
        showLoading("Removing pin...");
        try {
            await pinIncome(
                month,
                [{ incomeSourceId: payment.incomeSourceId, payment: null }],
                `Removed pin for ${payment.label}`,
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
        const payment = toPayment(newSource.payment);
        if (typeof payment === "string") {
            showError(payment);
            return;
        }
        if (newSource.owners.length === 0 || !newSource.label.trim() || !newSource.explanation.trim()) {
            showError("At least one owner, a label, and an explanation are required");
            return;
        }
        showLoading("Saving...");
        try {
            await createIncomeSource({
                owners: newSource.owners,
                label: newSource.label.trim(),
                frequency: newSource.frequency,
                initialPayment: payment,
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
        <div className="payment-editor">
            <PaymentForm draft={editDraft} onChange={setEditDraft} />
            <div className="edit-actions">
                <input
                    type="text"
                    className="explanation-inline"
                    value={editExplanation}
                    onChange={(e) => setEditExplanation(e.target.value)}
                    placeholder="Explanation (required)"
                    maxLength={MAX_EXPLANATION_LENGTH}
                />
                <button className="small-btn" onClick={() => handleSavePayment(incomeSourceId, label)}>
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
                <p className="hint">Future month: payments carry forward from earlier months. Edits create pins.</p>
            )}

            {income && (
                <>
                    <h2>
                        Planned net income: <span className="num">{formatCurrency(income.plannedNetIncome)}</span>
                    </h2>

                    <h2>Payments</h2>
                    {income.payments.length === 0 && sourcesWithoutPayment.length === 0 && (
                        <p className="hint">No income sources yet. Add one below.</p>
                    )}
                    {income.payments.map((p) => (
                        <div key={p.incomeSourceId} className={`payment-card ${p.active ? "" : "faded"}`}>
                            <h3>
                                {p.label}
                                <span className="muted"> — {ownerNames(p.owners)}</span>
                                {!p.active && <span className="muted"> (deactivated)</span>}
                                {p.pinned && <span className="badge badge-pinned">pinned</span>}
                            </h3>
                            {editingId === p.incomeSourceId ? (
                                renderEditor(p.incomeSourceId, p.label)
                            ) : (
                                <>
                                    <PaymentBreakdown payment={p} />
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
                        sourcesWithoutPayment.map((s) => (
                            <div key={s.incomeSourceId} className="payment-card">
                                <h3>
                                    {s.label}
                                    <span className="muted"> — {ownerNames(s.owners)}</span>
                                </h3>
                                {editingId === s.incomeSourceId ? (
                                    renderEditor(s.incomeSourceId, s.label)
                                ) : (
                                    <>
                                        <p className="hint">No payment in this month.</p>
                                        <button className="small-btn" onClick={() => startEdit(s.incomeSourceId, null)}>
                                            Add payment
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
                An income source is any stream of income that you plan, such as a job, interest, or dividends. A change
                here affects every month. A change to a payment goes in the month above.
            </p>
            <table className="data-table">
                <thead>
                    <tr>
                        <th>Label</th>
                        <th>Owners</th>
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
                            <td>{ownerNames(s.owners)}</td>
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
                <div className="add-form payment-editor">
                    <fieldset>
                        <legend>Owners (two or more for a joint source)</legend>
                        {people.map((p) => (
                            <label key={p.personId} className="checkbox-label">
                                <input
                                    type="checkbox"
                                    checked={newSource.owners.includes(p.personId)}
                                    onChange={(e) =>
                                        setNewSource({
                                            ...newSource,
                                            owners: e.target.checked
                                                ? [...newSource.owners, p.personId]
                                                : newSource.owners.filter((id) => id !== p.personId),
                                        })
                                    }
                                />
                                {p.name}
                            </label>
                        ))}
                    </fieldset>
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
                    <p className="hint">The first payment goes in the current month, and carries forward.</p>
                    <PaymentForm
                        draft={newSource.payment}
                        onChange={(payment) => setNewSource({ ...newSource, payment })}
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
