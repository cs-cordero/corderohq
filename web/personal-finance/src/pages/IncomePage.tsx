import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { useAdmin } from "../admin";
import {
    ApiError,
    createIncomeSource,
    createOneTimeIncome,
    deactivateIncomeSource,
    deleteOneTimeIncome,
    fetchAllIncomeSources,
    fetchIncomeMonth,
    fetchProfile,
    reactivateIncomeSource,
    replaceIncome,
    updateIncome,
    updateIncomeSource,
    updateOneTimeIncome,
} from "../api";
import LoadingOverlay from "../components/LoadingOverlay";
import MonthPicker from "../components/MonthPicker";
import ScopeChoice from "../components/ScopeChoice";
import StatusMessage from "../components/StatusMessage";
import { editability } from "../editability";
import { currentYearMonth, formatCurrency, parseCurrencyInput } from "../format";
import { useStatus } from "../hooks";
import {
    DEDUCTION_KIND_LABELS,
    DEDUCTION_KINDS,
    PAY_FREQUENCIES,
    PAY_FREQUENCY_LABELS,
    PAYMENTS_PER_MONTH,
    type ChangeScope,
    type Deduction,
    type DeductionKind,
    type IncomeMonth,
    type IncomeMonthOneTime,
    type IncomeMonthRecurring,
    type IncomeSource,
    type OneTimeIncome,
    type PayFrequency,
    type Person,
    type RecurringIncome,
    type RecurringIncomeChange,
} from "../types";

const MAX_EXPLANATION_LENGTH = 1000;
const YEAR_MONTH_PATTERN = /^\d{4}-(0[1-9]|1[0-2])$/;

// --- The money form ---------------------------------------------------------
//
// The form holds each amount as the text that the user types. toMoney() turns it into millionths,
// and returns an error message when a value is not an amount.

interface DeductionDraft {
    label: string;
    kind: DeductionKind;
    amount: string;
}

interface MoneyDraft {
    gross: string;
    deductions: DeductionDraft[];
}

interface Money {
    gross: number;
    deductions: Deduction[];
}

const EMPTY_MONEY: MoneyDraft = { gross: "", deductions: [] };

function toMoneyDraft(money: Money): MoneyDraft {
    return {
        gross: (money.gross / 1_000_000).toFixed(2),
        deductions: money.deductions.map((d) => ({
            label: d.label,
            kind: d.kind,
            amount: (d.amount / 1_000_000).toFixed(2),
        })),
    };
}

function toMoney(draft: MoneyDraft): Money | string {
    const gross = parseCurrencyInput(draft.gross);
    if (gross === null) return "Gross amount must be an amount";
    const deductions: Deduction[] = [];
    for (const d of draft.deductions) {
        const amount = parseCurrencyInput(d.amount);
        if (amount === null) return `The amount of "${d.label || "a deduction"}" must be an amount`;
        if (!d.label.trim()) return "Each deduction needs a label";
        deductions.push({ label: d.label.trim(), kind: d.kind, amount });
    }
    return { gross, deductions };
}

function draftNet(draft: MoneyDraft): number | null {
    const money = toMoney(draft);
    if (typeof money === "string") return null;
    return money.gross - money.deductions.reduce((sum, d) => sum + d.amount, 0);
}

function MoneyForm({ draft, onChange }: { draft: MoneyDraft; onChange: (draft: MoneyDraft) => void }) {
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
        </div>
    );
}

// --- The recurring income form ---------------------------------------------------

interface RecurringDraft {
    incomeSourceId: string;
    label: string;
    frequency: PayFrequency;
    money: MoneyDraft;
    endMonth: string; // YYYY-MM, or "" for no end
}

function toRecurringDraft(income: RecurringIncome): RecurringDraft {
    return {
        incomeSourceId: income.incomeSourceId,
        label: income.label,
        frequency: income.frequency,
        money: toMoneyDraft(income),
        endMonth: income.endMonth ?? "",
    };
}

function toRecurringIncome(draft: RecurringDraft): RecurringIncome | string {
    if (!draft.incomeSourceId) return "Choose an income source";
    if (!draft.label.trim()) return "A label is required";
    const endMonth = draft.endMonth.trim();
    if (endMonth && !YEAR_MONTH_PATTERN.test(endMonth)) return "The last month must be YYYY-MM";
    const money = toMoney(draft.money);
    if (typeof money === "string") return money;
    const income: RecurringIncome = {
        incomeSourceId: draft.incomeSourceId,
        label: draft.label.trim(),
        frequency: draft.frequency,
        ...money,
    };
    if (endMonth) income.endMonth = endMonth;
    return income;
}

// The fields of a recurring income that do not depend on the source: the label, the frequency, the
// money, and (from this month on only) the last month.
function RecurringForm({
    draft,
    onChange,
    showEndMonth,
}: {
    draft: RecurringDraft;
    onChange: (draft: RecurringDraft) => void;
    showEndMonth: boolean;
}) {
    return (
        <>
            <div className="add-form">
                <input
                    type="text"
                    placeholder="Label, such as Salary or Rent of the lake house"
                    value={draft.label}
                    onChange={(e) => onChange({ ...draft, label: e.target.value })}
                />
                <select
                    value={draft.frequency}
                    onChange={(e) => onChange({ ...draft, frequency: e.target.value as PayFrequency })}
                >
                    {PAY_FREQUENCIES.map((f) => (
                        <option key={f} value={f}>
                            {PAY_FREQUENCY_LABELS[f]}
                        </option>
                    ))}
                </select>
                {showEndMonth && (
                    <label>
                        Last month (optional){" "}
                        <input
                            type="text"
                            placeholder="YYYY-MM"
                            value={draft.endMonth}
                            onChange={(e) => onChange({ ...draft, endMonth: e.target.value })}
                        />
                    </label>
                )}
            </div>
            <MoneyForm draft={draft.money} onChange={(money) => onChange({ ...draft, money })} />
        </>
    );
}

// --- The read-only breakdown --------------------------------------------------

function MoneyBreakdown({
    money,
    net,
    frequency,
    monthlyNet,
}: {
    money: Money;
    net: number;
    frequency?: PayFrequency;
    monthlyNet?: number;
}) {
    const perMonth = frequency ? PAYMENTS_PER_MONTH[frequency] : null;
    return (
        <table className="data-table payment-breakdown">
            <tbody>
                <tr>
                    <td>Gross amount</td>
                    <td />
                    <td className="num">{formatCurrency(money.gross)}</td>
                </tr>
                {money.deductions.map((d, i) => (
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
                        <strong>{formatCurrency(net)}</strong>
                    </td>
                </tr>
                {perMonth !== null && monthlyNet !== undefined && (
                    <tr>
                        <td>
                            × {perMonth} payment{perMonth !== 1 ? "s" : ""} a month
                        </td>
                        <td />
                        <td className="num">
                            <strong>{formatCurrency(monthlyNet)}</strong>
                        </td>
                    </tr>
                )}
            </tbody>
        </table>
    );
}

// One checkbox for each person. A joint source has two or more owners.
function OwnerCheckboxes({
    people,
    owners,
    onChange,
}: {
    people: Person[];
    owners: string[];
    onChange: (owners: string[]) => void;
}) {
    return (
        <>
            {people.map((p) => (
                <label key={p.personId} className="checkbox-label">
                    <input
                        type="checkbox"
                        checked={owners.includes(p.personId)}
                        onChange={(e) =>
                            onChange(
                                e.target.checked ? [...owners, p.personId] : owners.filter((id) => id !== p.personId),
                            )
                        }
                    />
                    {p.name}
                </label>
            ))}
        </>
    );
}

// --- The page -----------------------------------------------------------------

interface SourceDraft {
    id: string | null; // null for a new source
    label: string;
    owners: string[];
}

interface OneTimeDraft {
    plannedIncomeId: string | null; // null for a new one-time income
    label: string;
    incomeSourceId: string; // "" for no source
    money: MoneyDraft;
    explanation: string;
}

interface PendingDeactivate {
    source: IncomeSource;
    explanation: string;
    // The own values in later months that the deactivation drops. Null until the server lists them.
    futureValues: { yearMonth: string; plannedIncomeId: string }[] | null;
}

// The future values of a 409 from the deactivation of an income source, or null for another error.
function futureValuesOf(err: unknown): { yearMonth: string; plannedIncomeId: string }[] | null {
    if (!(err instanceof ApiError) || err.status !== 409) return null;
    try {
        const body: unknown = JSON.parse(err.responseBody);
        if (typeof body !== "object" || body === null || !("futureValues" in body)) return null;
        const values = (body as { futureValues: unknown }).futureValues;
        if (!Array.isArray(values)) return null;
        return values.map((v: { yearMonth?: unknown; plannedIncomeId?: unknown }) => ({
            yearMonth: String(v.yearMonth),
            plannedIncomeId: String(v.plannedIncomeId),
        }));
    } catch {
        return null;
    }
}

export default function IncomePage() {
    const [month, setMonth] = useState(currentYearMonth);
    const [income, setIncome] = useState<IncomeMonth | null>(null);
    const [sources, setSources] = useState<IncomeSource[]>([]);
    const [people, setPeople] = useState<Person[]>([]);
    const { status, showLoading, showSuccess, showError, clear } = useStatus();
    const { adminModeOn } = useAdmin();

    // The recurring income that is open for edit, by plannedIncomeId. "new" is a new recurring income.
    const [editingId, setEditingId] = useState<string | null>(null);
    const [editDraft, setEditDraft] = useState<RecurringDraft | null>(null);
    const [editExplanation, setEditExplanation] = useState("");
    const [editScope, setEditScope] = useState<ChangeScope>("FORWARD");

    const [oneTimeDraft, setOneTimeDraft] = useState<OneTimeDraft | null>(null);
    const [sourceDraft, setSourceDraft] = useState<SourceDraft | null>(null);
    const [pendingDeactivate, setPendingDeactivate] = useState<PendingDeactivate | null>(null);

    const editState = editability(month);
    const isLocked = editState === "LOCKED";
    const isReadOnly = isLocked && !adminModeOn;
    const willOverride = isLocked && adminModeOn;
    // A future month is sparse, the same as the budget: it goes through /update, not /replace.
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
            setEditDraft(null);
            setOneTimeDraft(null);
            clear();
        } catch (err) {
            showError(err);
        }
    }, [month]); // eslint-disable-line react-hooks/exhaustive-deps

    useEffect(() => {
        load();
    }, [load]);

    const activeSources = useMemo(() => sources.filter((s) => s.active), [sources]);

    // The names of the owners of a source. A joint source has two or more.
    const ownerNames = useCallback(
        (owners: string[]) =>
            owners.map((id) => people.find((p) => p.personId === id)?.name ?? "Unknown person").join(" & "),
        [people],
    );

    // The recurring incomes of the month, in groups by income source. The server sorts them already.
    const recurringBySource = useMemo(() => {
        const groups: {
            incomeSourceId: string;
            sourceLabel: string;
            owners: string[];
            items: IncomeMonthRecurring[];
        }[] = [];
        for (const r of income?.recurring ?? []) {
            const last = groups[groups.length - 1];
            if (last && last.incomeSourceId === r.incomeSourceId) {
                last.items.push(r);
            } else {
                groups.push({
                    incomeSourceId: r.incomeSourceId,
                    sourceLabel: r.sourceLabel,
                    owners: r.owners,
                    items: [r],
                });
            }
        }
        return groups;
    }, [income]);

    // An end in a month before the current month is locked. Only the admin override can change it.
    const isEndLocked = (r: IncomeMonthRecurring) => {
        const endMonth = r.carriedRecurringIncome?.endMonth;
        return endMonth !== undefined && endMonth < currentYearMonth() && !willOverride;
    };

    const startEdit = (r: IncomeMonthRecurring) => {
        setEditingId(r.plannedIncomeId);
        setEditDraft(toRecurringDraft(r));
        setEditExplanation("");
        setEditScope(r.hasPinnedRecurringIncome ? "THIS_MONTH" : "FORWARD");
    };

    const startNew = () => {
        setEditingId("new");
        setEditDraft({
            incomeSourceId: activeSources[0]?.incomeSourceId ?? "",
            label: "",
            frequency: "biweekly",
            money: EMPTY_MONEY,
            endMonth: "",
        });
        setEditExplanation("");
        setEditScope("FORWARD");
    };

    const cancelEdit = () => {
        setEditingId(null);
        setEditDraft(null);
        setEditExplanation("");
    };

    // A dense month (the current, grace, or a locked month) is written whole through /replace: the
    // body sends the carried value of each recurring income again, and the server skips each value
    // that did not change. `changes` come after them, so they win.
    const replaceWith = (changes: RecurringIncomeChange[]): RecurringIncomeChange[] => [
        ...(income?.recurring ?? []).flatMap((r): RecurringIncomeChange[] =>
            r.carriedRecurringIncome === null
                ? []
                : [{ action: "SET", plannedIncomeId: r.plannedIncomeId, recurringIncome: r.carriedRecurringIncome }],
        ),
        ...changes,
    ];

    // Send the changes of this month: /update for a future month, else /replace. Return the
    // later carried changes of /update for `plannedIncomeId`, so that the caller can offer to clear them.
    const writeChanges = async (
        changes: RecurringIncomeChange[],
        explanation: string,
        plannedIncomeId: string | null,
    ): Promise<{ yearMonth: string }[]> => {
        if (isFutureMonth) {
            const result = await updateIncome(month, changes, explanation);
            setIncome(result.incomePlan);
            return plannedIncomeId === null
                ? []
                : result.warnings.laterCarriedChanges.filter((c) => c.plannedIncomeId === plannedIncomeId);
        }
        setIncome(await replaceIncome(month, replaceWith(changes), explanation, willOverride));
        return [];
    };

    const handleSaveRecurring = async () => {
        if (!income || !editDraft || editingId === null) return;
        const value = toRecurringIncome(editDraft);
        if (typeof value === "string") {
            showError(value);
            return;
        }
        const explanation = editExplanation.trim();
        if (!explanation) {
            showError("Explanation is required");
            return;
        }
        const isNew = editingId === "new";
        const current = income.recurring.find((r) => r.plannedIncomeId === editingId);
        const forward = isNew || editScope === "FORWARD";

        let changes: RecurringIncomeChange[];
        if (isNew) {
            changes = [{ action: "SET", recurringIncome: value }];
        } else if (forward) {
            // "From this month on" sets the carried value, and clears a value for this month only.
            changes = [{ action: "SET", plannedIncomeId: editingId, recurringIncome: value }];
            if (current?.hasPinnedRecurringIncome) changes.push({ action: "CLEAR_PIN", plannedIncomeId: editingId });
        } else {
            // "This month only" keeps the end month of the carried value.
            const endMonth = current?.carriedRecurringIncome?.endMonth;
            const pinned: RecurringIncome = { ...value };
            delete pinned.endMonth;
            if (endMonth !== undefined) pinned.endMonth = endMonth;
            changes = [{ action: "SET_PIN", plannedIncomeId: editingId, recurringIncome: pinned }];
        }

        showLoading("Saving...");
        try {
            const later = await writeChanges(changes, explanation, isNew || !forward ? null : editingId);
            cancelEdit();
            if (later.length > 0) {
                const monthsList = later.map((c) => c.yearMonth).join(", ");
                const shouldClear = window.confirm(
                    `${value.label} has changes in later months that won't see this value: ${monthsList}. ` +
                        `Clear those later changes so this value carries forward?`,
                );
                if (shouldClear) {
                    for (const c of later) {
                        await updateIncome(
                            c.yearMonth,
                            [{ action: "CLEAR", plannedIncomeId: editingId }],
                            `Clearing later changes after a change of ${value.label} in ${month}`,
                        );
                    }
                    await load();
                    showSuccess("Saved; later changes cleared");
                } else {
                    showSuccess("Saved; later changes kept");
                }
            } else {
                showSuccess(willOverride ? "Saved with override" : "Saved");
            }
            setTimeout(clear, 4000);
        } catch (err) {
            showError(err);
        }
    };

    // Undo the change of this month: a value for this month only first, else the own carried value
    // of a future month. The month then shows the value carried from earlier months again.
    const handleUndo = async (r: IncomeMonthRecurring) => {
        const what = r.hasPinnedRecurringIncome ? "the this-month-only change" : "the change in this month";
        if (!window.confirm(`Undo ${what} for ${r.label}?`)) return;
        const change: RecurringIncomeChange = r.hasPinnedRecurringIncome
            ? { action: "CLEAR_PIN", plannedIncomeId: r.plannedIncomeId }
            : { action: "CLEAR", plannedIncomeId: r.plannedIncomeId };
        showLoading("Undoing...");
        try {
            await writeChanges([change], `Undo ${what} for ${r.label}`, null);
            showSuccess("Change undone");
            setTimeout(clear, 3000);
        } catch (err) {
            showError(err);
        }
    };

    const handleSaveOneTime = async () => {
        if (!oneTimeDraft) return;
        const money = toMoney(oneTimeDraft.money);
        if (typeof money === "string") {
            showError(money);
            return;
        }
        if (!oneTimeDraft.label.trim()) {
            showError("A label is required");
            return;
        }
        const explanation = oneTimeDraft.explanation.trim();
        if (!explanation) {
            showError("Explanation is required");
            return;
        }
        const value: OneTimeIncome = { label: oneTimeDraft.label.trim(), ...money };
        if (oneTimeDraft.incomeSourceId) value.incomeSourceId = oneTimeDraft.incomeSourceId;
        showLoading("Saving...");
        try {
            if (oneTimeDraft.plannedIncomeId === null) {
                await createOneTimeIncome(month, value, explanation, willOverride);
            } else {
                await updateOneTimeIncome(month, oneTimeDraft.plannedIncomeId, value, explanation, willOverride);
            }
            await load();
            showSuccess("One-time income saved");
            setTimeout(clear, 3000);
        } catch (err) {
            showError(err);
        }
    };

    const handleDeleteOneTime = async (item: IncomeMonthOneTime) => {
        const explanation = window.prompt(`Delete "${item.label}"? Give an explanation:`);
        if (!explanation || !explanation.trim()) return;
        showLoading("Deleting...");
        try {
            await deleteOneTimeIncome(month, item.plannedIncomeId, explanation.trim(), willOverride);
            await load();
            showSuccess("One-time income deleted");
            setTimeout(clear, 3000);
        } catch (err) {
            showError(err);
        }
    };

    const handleSaveSource = async () => {
        if (!sourceDraft) return;
        if (!sourceDraft.label.trim() || sourceDraft.owners.length === 0) {
            showError("A label and at least one owner are required");
            return;
        }
        const data = { label: sourceDraft.label.trim(), owners: sourceDraft.owners };
        try {
            if (sourceDraft.id === null) {
                await createIncomeSource(data);
            } else {
                await updateIncomeSource(sourceDraft.id, data);
            }
            setSourceDraft(null);
            await load();
            showSuccess(sourceDraft.id === null ? "Income source added" : "Saved");
            setTimeout(clear, 3000);
        } catch (err) {
            showError(err);
        }
    };

    // The deactivation ends each recurring income of the source in the current month. A source
    // with own values in later months needs a confirmation, and the server lists those values.
    const handleConfirmDeactivate = async () => {
        if (!pendingDeactivate) return;
        const explanation = pendingDeactivate.explanation.trim();
        if (!explanation) {
            showError("Explanation is required");
            return;
        }
        try {
            const result = await deactivateIncomeSource(pendingDeactivate.source.incomeSourceId, {
                confirm: pendingDeactivate.futureValues !== null,
                explanation,
            });
            setPendingDeactivate(null);
            await load();
            const ended = result.endedRecurringIncomes.length;
            showSuccess(
                `Deactivated ${pendingDeactivate.source.label}. ${ended} recurring income(s) end in ${currentYearMonth()}.`,
            );
        } catch (err) {
            const futureValues = futureValuesOf(err);
            if (futureValues !== null) {
                setPendingDeactivate({ ...pendingDeactivate, futureValues });
                return;
            }
            showError(err);
        }
    };

    const handleReactivate = async (source: IncomeSource) => {
        try {
            await reactivateIncomeSource(source.incomeSourceId);
            await load();
            showSuccess(`Reactivated ${source.label}. Its ended recurring incomes stay ended.`);
        } catch (err) {
            showError(err);
        }
    };

    const saveLabel = willOverride ? "Save with override" : "Save";
    const labelOfIncome = (plannedIncomeId: string) =>
        income?.recurring.find((r) => r.plannedIncomeId === plannedIncomeId)?.label ?? plannedIncomeId;

    const renderEditor = (isNew: boolean) =>
        editDraft && (
            <div className="payment-editor">
                {isNew && (
                    <div className="add-form">
                        <label>
                            Income source{" "}
                            <select
                                value={editDraft.incomeSourceId}
                                onChange={(e) => setEditDraft({ ...editDraft, incomeSourceId: e.target.value })}
                            >
                                {activeSources.map((s) => (
                                    <option key={s.incomeSourceId} value={s.incomeSourceId}>
                                        {s.label}
                                    </option>
                                ))}
                            </select>
                        </label>
                        <span className="hint">It starts in {month}, and carries forward.</span>
                    </div>
                )}
                <RecurringForm
                    draft={editDraft}
                    onChange={setEditDraft}
                    showEndMonth={isNew || editScope === "FORWARD"}
                />
                <div className="edit-actions">
                    {!isNew && <ScopeChoice value={editScope} onChange={setEditScope} />}
                    <input
                        type="text"
                        className="explanation-inline"
                        value={editExplanation}
                        onChange={(e) => setEditExplanation(e.target.value)}
                        placeholder="Explanation (required)"
                        maxLength={MAX_EXPLANATION_LENGTH}
                    />
                    <button className="small-btn" onClick={handleSaveRecurring}>
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
                <p className="hint">
                    Future month: recurring incomes carry forward from earlier months. A change applies from this month
                    on, or to this month only.
                </p>
            )}

            {income && (
                <>
                    <h2>
                        Planned net income: <span className="num">{formatCurrency(income.plannedNetIncome)}</span>
                    </h2>

                    <h2>Recurring income</h2>
                    {income.recurring.length === 0 && <p className="hint">No recurring income in this month.</p>}
                    {recurringBySource.map((group) => (
                        <Fragment key={group.incomeSourceId}>
                            <h3>
                                {group.sourceLabel}
                                <span className="muted"> — {ownerNames(group.owners)}</span>
                            </h3>
                            {group.items.map((r) => (
                                <div key={r.plannedIncomeId} className="payment-card">
                                    <h4>
                                        {r.label}
                                        <span className="muted"> — {PAY_FREQUENCY_LABELS[r.frequency]}</span>
                                        {isFutureMonth && r.inherited === false && (
                                            <span className="badge badge-pinned">changes here</span>
                                        )}
                                        {r.hasPinnedRecurringIncome && (
                                            <span className="badge badge-pinned">this month only</span>
                                        )}
                                        {r.endMonth !== undefined && (
                                            <span className="badge">
                                                {r.endMonth === month ? "ends this month" : `ends ${r.endMonth}`}
                                            </span>
                                        )}
                                    </h4>
                                    {editingId === r.plannedIncomeId ? (
                                        renderEditor(false)
                                    ) : (
                                        <>
                                            <MoneyBreakdown
                                                money={r}
                                                net={r.net}
                                                frequency={r.frequency}
                                                monthlyNet={r.monthlyNet}
                                            />
                                            {!isReadOnly && !isEndLocked(r) && (
                                                <div className="edit-actions">
                                                    <button className="small-btn" onClick={() => startEdit(r)}>
                                                        Edit
                                                    </button>
                                                    {(r.hasPinnedRecurringIncome ||
                                                        (isFutureMonth && r.inherited === false)) && (
                                                        <button className="small-btn" onClick={() => handleUndo(r)}>
                                                            Undo change
                                                        </button>
                                                    )}
                                                </div>
                                            )}
                                            {!isReadOnly && isEndLocked(r) && (
                                                <p className="hint">
                                                    This income ended in {r.carriedRecurringIncome?.endMonth}. Only an
                                                    admin can change it.
                                                </p>
                                            )}
                                        </>
                                    )}
                                </div>
                            ))}
                        </Fragment>
                    ))}
                    {!isReadOnly &&
                        (editingId === "new" ? (
                            <div className="payment-card">{renderEditor(true)}</div>
                        ) : activeSources.length === 0 ? (
                            <p className="hint">Add an income source below before you add a recurring income.</p>
                        ) : (
                            <button className="small-btn" onClick={startNew}>
                                + Add recurring income
                            </button>
                        ))}

                    <h2>One-time income</h2>
                    <p className="hint">
                        A bonus, an RSU vest, a tax refund, a third paycheck, or other extra money that you want to
                        budget. It counts in this month only.
                    </p>
                    <table className="data-table">
                        <tbody>
                            {income.oneTime.map((item) => (
                                <tr key={item.plannedIncomeId}>
                                    <td>
                                        {item.label}
                                        {item.sourceLabel && <span className="muted"> — {item.sourceLabel}</span>}
                                    </td>
                                    <td className="num">{formatCurrency(item.net)}</td>
                                    <td className="actions">
                                        {!isReadOnly && (
                                            <>
                                                <button
                                                    className="small-btn"
                                                    onClick={() =>
                                                        setOneTimeDraft({
                                                            plannedIncomeId: item.plannedIncomeId,
                                                            label: item.label,
                                                            incomeSourceId: item.incomeSourceId ?? "",
                                                            money: toMoneyDraft(item),
                                                            explanation: "",
                                                        })
                                                    }
                                                >
                                                    Edit
                                                </button>
                                                <button
                                                    className="small-btn delete-btn"
                                                    onClick={() => handleDeleteOneTime(item)}
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
                            onClick={() =>
                                setOneTimeDraft({
                                    plannedIncomeId: null,
                                    label: "",
                                    incomeSourceId: "",
                                    money: EMPTY_MONEY,
                                    explanation: "",
                                })
                            }
                        >
                            + Add one-time income
                        </button>
                    )}
                    {oneTimeDraft && (
                        <div className="payment-editor">
                            <div className="add-form">
                                <input
                                    type="text"
                                    placeholder="Label"
                                    value={oneTimeDraft.label}
                                    onChange={(e) => setOneTimeDraft({ ...oneTimeDraft, label: e.target.value })}
                                />
                                <select
                                    value={oneTimeDraft.incomeSourceId}
                                    onChange={(e) =>
                                        setOneTimeDraft({ ...oneTimeDraft, incomeSourceId: e.target.value })
                                    }
                                >
                                    <option value="">No income source</option>
                                    {sources
                                        .filter((s) => s.active || s.incomeSourceId === oneTimeDraft.incomeSourceId)
                                        .map((s) => (
                                            <option key={s.incomeSourceId} value={s.incomeSourceId}>
                                                {s.label}
                                            </option>
                                        ))}
                                </select>
                            </div>
                            <MoneyForm
                                draft={oneTimeDraft.money}
                                onChange={(money) => setOneTimeDraft({ ...oneTimeDraft, money })}
                            />
                            <div className="edit-actions">
                                <input
                                    type="text"
                                    className="explanation-inline"
                                    placeholder="Explanation (required)"
                                    maxLength={MAX_EXPLANATION_LENGTH}
                                    value={oneTimeDraft.explanation}
                                    onChange={(e) => setOneTimeDraft({ ...oneTimeDraft, explanation: e.target.value })}
                                />
                                <button className="primary-btn" onClick={handleSaveOneTime}>
                                    {saveLabel}
                                </button>
                                <button className="small-btn" onClick={() => setOneTimeDraft(null)}>
                                    Cancel
                                </button>
                            </div>
                        </div>
                    )}
                </>
            )}

            <h2>Income sources</h2>
            <p className="hint">
                An income source is who pays the household, such as a job, a bank, or a landlord business. It holds no
                money. Its recurring and one-time incomes go in the month above.
            </p>
            <table className="data-table">
                <thead>
                    <tr>
                        <th>Label</th>
                        <th>Owners</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {sources.map((s) => (
                        <tr key={s.incomeSourceId} className={s.active ? "" : "faded"}>
                            {sourceDraft?.id === s.incomeSourceId ? (
                                <>
                                    <td>
                                        <input
                                            type="text"
                                            value={sourceDraft.label}
                                            onChange={(e) => setSourceDraft({ ...sourceDraft, label: e.target.value })}
                                        />
                                    </td>
                                    <td>
                                        <OwnerCheckboxes
                                            people={people}
                                            owners={sourceDraft.owners}
                                            onChange={(owners) => setSourceDraft({ ...sourceDraft, owners })}
                                        />
                                    </td>
                                    <td className="actions">
                                        <button className="small-btn" onClick={handleSaveSource}>
                                            Save
                                        </button>
                                        <button className="small-btn" onClick={() => setSourceDraft(null)}>
                                            Cancel
                                        </button>
                                    </td>
                                </>
                            ) : (
                                <>
                                    <td>
                                        {s.label}
                                        {!s.active && <span className="muted"> (deactivated)</span>}
                                    </td>
                                    <td>{ownerNames(s.owners)}</td>
                                    <td className="actions">
                                        <button
                                            className="small-btn"
                                            onClick={() =>
                                                setSourceDraft({
                                                    id: s.incomeSourceId,
                                                    label: s.label,
                                                    owners: [...s.owners],
                                                })
                                            }
                                        >
                                            Edit
                                        </button>
                                        {s.active ? (
                                            <button
                                                className="small-btn delete-btn"
                                                onClick={() =>
                                                    setPendingDeactivate({
                                                        source: s,
                                                        explanation: "",
                                                        futureValues: null,
                                                    })
                                                }
                                            >
                                                Deactivate
                                            </button>
                                        ) : (
                                            <button className="small-btn" onClick={() => handleReactivate(s)}>
                                                Reactivate
                                            </button>
                                        )}
                                    </td>
                                </>
                            )}
                        </tr>
                    ))}
                </tbody>
            </table>

            {pendingDeactivate && (
                <div className="add-form">
                    <p>
                        Deactivate {pendingDeactivate.source.label}? Each of its recurring incomes ends in{" "}
                        {currentYearMonth()}.
                        {pendingDeactivate.futureValues !== null &&
                            ` It also drops the changes in later months: ${pendingDeactivate.futureValues
                                .map((v) => `${labelOfIncome(v.plannedIncomeId)} in ${v.yearMonth}`)
                                .join(", ")}.`}
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
                        {pendingDeactivate.futureValues === null ? "Deactivate" : "Deactivate and drop the changes"}
                    </button>
                    <button className="small-btn" onClick={() => setPendingDeactivate(null)}>
                        Cancel
                    </button>
                </div>
            )}

            {sourceDraft === null || sourceDraft.id !== null ? (
                <button className="primary-btn" onClick={() => setSourceDraft({ id: null, label: "", owners: [] })}>
                    + Add income source
                </button>
            ) : (
                <div className="add-form">
                    <fieldset>
                        <legend>Owners (two or more for a joint source)</legend>
                        <OwnerCheckboxes
                            people={people}
                            owners={sourceDraft.owners}
                            onChange={(owners) => setSourceDraft({ ...sourceDraft, owners })}
                        />
                    </fieldset>
                    <input
                        type="text"
                        placeholder="Label, such as the employer"
                        value={sourceDraft.label}
                        onChange={(e) => setSourceDraft({ ...sourceDraft, label: e.target.value })}
                    />
                    <button className="primary-btn" onClick={handleSaveSource}>
                        Add
                    </button>
                    <button className="small-btn" onClick={() => setSourceDraft(null)}>
                        Cancel
                    </button>
                </div>
            )}
        </div>
    );
}
