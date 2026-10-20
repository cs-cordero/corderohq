import { useCallback, useEffect, useMemo, useState } from "react";
import { DndContext, type DragEndEvent, PointerSensor, closestCenter, useSensor, useSensors } from "@dnd-kit/core";
import { SortableContext, arrayMove, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import {
    ApiError,
    createAccount,
    createPerson,
    deactivateAccount,
    deletePerson,
    fetchAllAccounts,
    fetchProfile,
    reactivateAccount,
    reorderAccounts,
    updateAccount,
    updatePerson,
} from "../api";
import LoadingOverlay from "../components/LoadingOverlay";
import MonthYearInput from "../components/MonthYearInput";
import StatusMessage from "../components/StatusMessage";
import { formatYearMonth, parseCurrencyInput } from "../format";
import { useStatus } from "../hooks";
import {
    ACCOUNT_TYPE_LABELS,
    ACCOUNT_TYPE_META,
    ACCOUNT_TYPES,
    ASSET_CLASS_LABELS,
    ASSET_CLASSES,
    TARGET_YEAR_VINTAGES,
    type Account,
    type AccountCreate,
    type AccountUpdate,
    type LoanTerms,
    type Person,
} from "../types";

// --- Form state -------------------------------------------------------------

// The account TYPE sets everything else: the liability flag, whether the asset class is
// fixed, and whether the loan-term fields appear. So the form holds the type, and it reads
// the rest from ACCOUNT_TYPE_META. It keeps no separate copy of those values.
interface AccountFormState {
    name: string;
    accountType: string;
    assetClasses: string[]; // the set the account holds (choose types); auto-forced for fixed types
    targetYear: number | null; // required iff assetClasses includes "target_date"
    owners: string[]; // personIds; at least one required at submit
    excludedFromNetWorth: boolean;
    notes: string;
    loanRatePct: string; // e.g. "4.875" (percent) — converted to a decimal on submit
    loanPayment: string; // dollars, e.g. "2500.00"
    loanPayoff: string; // YYYY-MM
}

function emptyForm(): AccountFormState {
    const t = ACCOUNT_TYPES[0]; // "checking"
    const fixed = ACCOUNT_TYPE_META[t].fixedAssetClass;
    return {
        name: "",
        accountType: t,
        assetClasses: fixed ? [fixed] : [],
        targetYear: null,
        owners: [],
        excludedFromNetWorth: false,
        notes: "",
        loanRatePct: "",
        loanPayment: "",
        loanPayoff: "",
    };
}

function formFromAccount(account: Account): AccountFormState {
    const lt = account.loanTerms;
    return {
        name: account.name,
        accountType: account.accountType,
        assetClasses: [...account.assetClasses],
        targetYear: account.targetYear ?? null,
        owners: [...account.owners],
        excludedFromNetWorth: account.excludedFromNetWorth,
        notes: account.notes ?? "",
        // Strip float noise from rate*100 (0.04875 -> "4.875").
        loanRatePct: lt ? String(+(lt.interestRate * 100).toFixed(6)) : "",
        loanPayment: lt ? (lt.monthlyPayment / 1_000_000).toFixed(2) : "",
        loanPayoff: lt ? lt.payoffYearMonth : "",
    };
}

// Build the loanTerms value from the form. Returns a LoanTerms when all three
// fields are present, null when none are (meaning "no/remove loan terms"), or
// throws when partially filled (the backend requires all-or-nothing). Only
// amortizing types offer the fields.
function buildLoanTerms(form: AccountFormState, amortizing: boolean): LoanTerms | null {
    if (!amortizing) return null;
    const rate = form.loanRatePct.trim();
    const payment = form.loanPayment.trim();
    const payoff = form.loanPayoff.trim();
    const filled = [rate, payment, payoff].filter((v) => v !== "");
    if (filled.length === 0) return null;
    if (filled.length < 3) {
        throw new Error(
            "Loan terms are all-or-nothing: fill in rate, monthly payment, and payoff month (or leave all blank).",
        );
    }
    const ratePct = parseFloat(rate);
    if (isNaN(ratePct) || ratePct < 0 || ratePct >= 100) {
        throw new Error("Interest rate must be a percentage in [0, 100), e.g. 4.875");
    }
    const paymentMillionths = parseCurrencyInput(payment);
    if (paymentMillionths === null || paymentMillionths < 0) {
        throw new Error("Monthly payment must be a non-negative dollar amount");
    }
    if (!/^\d{4}-\d{2}$/.test(payoff)) {
        throw new Error("Payoff month must be a valid YYYY-MM");
    }
    return {
        // Convert percent -> decimal; round to shed binary-float noise (4.875 -> 0.04875).
        interestRate: +(ratePct / 100).toFixed(8),
        monthlyPayment: paymentMillionths,
        payoffYearMonth: payoff,
    };
}

// --- Sortable row -----------------------------------------------------------

function SortableAccountRow({ account, children }: { account: Account; children: React.ReactNode }) {
    const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
        id: account.accountId,
    });
    return (
        <tr
            ref={setNodeRef}
            style={{ transform: CSS.Transform.toString(transform), transition, opacity: isDragging ? 0.5 : 1 }}
        >
            <td className="drag-handle-cell">
                <button className="drag-handle" title="Drag to reorder" {...attributes} {...listeners}>
                    ⋮⋮
                </button>
            </td>
            {children}
        </tr>
    );
}

// --- Page -------------------------------------------------------------------

export default function AccountsPage() {
    const [accounts, setAccounts] = useState<Account[]>([]);
    const [people, setPeople] = useState<Person[]>([]);
    const { status, showLoading, showError, showSuccess, clear } = useStatus();

    // Account create/edit form. editingId === null means "create" mode; the form
    // is hidden until the user opens it (+ Add account) or edits a row.
    const [showAccountForm, setShowAccountForm] = useState(false);
    const [editingId, setEditingId] = useState<string | null>(null);
    const [form, setForm] = useState<AccountFormState>(emptyForm());

    // Household (people) panel state.
    const [showPersonForm, setShowPersonForm] = useState(false);
    const [personName, setPersonName] = useState("");
    const [personBirth, setPersonBirth] = useState("");
    const [editingPersonId, setEditingPersonId] = useState<string | null>(null);
    const [editPersonName, setEditPersonName] = useState("");
    const [editPersonBirth, setEditPersonBirth] = useState("");

    const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 4 } }));

    const load = useCallback(async () => {
        showLoading("Loading...");
        try {
            const [a, p] = await Promise.all([fetchAllAccounts(), fetchProfile()]);
            setAccounts(a);
            setPeople(p);
            clear();
        } catch (err) {
            showError(err);
        }
    }, []); // eslint-disable-line react-hooks/exhaustive-deps

    useEffect(() => {
        load();
    }, [load]);

    const peopleById = useMemo(() => new Map(people.map((p) => [p.personId, p])), [people]);

    const activeAccounts = useMemo(
        () =>
            accounts.filter((a) => a.active).sort((x, y) => x.sortOrder - y.sortOrder || x.name.localeCompare(y.name)),
        [accounts],
    );
    const inactiveAccounts = useMemo(
        () => accounts.filter((a) => !a.active).sort((x, y) => x.name.localeCompare(y.name)),
        [accounts],
    );

    const ownersLabel = (owners: string[]): string => {
        if (!owners || owners.length === 0) return "—";
        return owners.map((id) => peopleById.get(id)?.name ?? `<missing: ${id}>`).join(", ");
    };

    // Behavior for the currently-selected type drives which fields render.
    const meta = ACCOUNT_TYPE_META[form.accountType];

    const closeForm = () => {
        setShowAccountForm(false);
        setEditingId(null);
        setForm(emptyForm());
    };

    const openCreate = () => {
        setEditingId(null);
        setForm(emptyForm());
        setShowAccountForm(true);
    };

    const startEdit = (account: Account) => {
        setEditingId(account.accountId);
        setForm(formFromAccount(account));
        setShowAccountForm(true);
        window.scrollTo({ top: 0, behavior: "smooth" });
    };

    // Changing the type re-derives dependent fields: force the fixed asset class,
    // clear a now-irrelevant target year, and drop loan-term entries the new type
    // doesn't allow.
    const changeAccountType = (accountType: string) => {
        const m = ACCOUNT_TYPE_META[accountType];
        setForm((f) => {
            const assetClasses = m.fixedAssetClass ? [m.fixedAssetClass] : f.assetClasses;
            return {
                ...f,
                accountType,
                assetClasses,
                targetYear: assetClasses.includes("target_date") ? f.targetYear : null,
                loanRatePct: m.amortizing ? f.loanRatePct : "",
                loanPayment: m.amortizing ? f.loanPayment : "",
                loanPayoff: m.amortizing ? f.loanPayoff : "",
            };
        });
    };

    const toggleAssetClass = (assetClass: string, checked: boolean) => {
        setForm((f) => {
            const assetClasses = checked
                ? [...f.assetClasses, assetClass]
                : f.assetClasses.filter((c) => c !== assetClass);
            return {
                ...f,
                assetClasses,
                // Clear the target year when Target Date is unchecked.
                targetYear: assetClasses.includes("target_date") ? f.targetYear : null,
            };
        });
    };

    const toggleOwner = (personId: string, checked: boolean) => {
        setForm((f) => ({
            ...f,
            owners: checked ? [...f.owners, personId] : f.owners.filter((id) => id !== personId),
        }));
    };

    const handleSubmit = async () => {
        const name = form.name.trim();
        if (!name) {
            showError("Account name is required");
            return;
        }
        if (form.owners.length === 0) {
            showError("Select at least one owner");
            return;
        }
        // Asset class is user-chosen only for "choose" types (not fixed, not a liability).
        const assetClassEditable = !meta.liability && meta.fixedAssetClass === null;
        if (assetClassEditable && form.assetClasses.length === 0) {
            showError("Select at least one asset class");
            return;
        }
        if (form.assetClasses.includes("target_date") && form.targetYear === null) {
            showError("Pick a target year for the Target Date allocation");
            return;
        }
        let loanTerms: LoanTerms | null;
        try {
            loanTerms = buildLoanTerms(form, meta.amortizing);
        } catch (err) {
            showError(err);
            return;
        }

        // Only send a targetYear when target_date is among the classes; otherwise the
        // server rejects it (and clears any stale one on update).
        const targetYear = form.assetClasses.includes("target_date") ? form.targetYear : null;

        try {
            if (editingId === null) {
                const data: AccountCreate = {
                    name,
                    accountType: form.accountType,
                    assetClasses: form.assetClasses,
                    owners: form.owners,
                    excludedFromNetWorth: form.excludedFromNetWorth,
                };
                if (targetYear !== null) data.targetYear = targetYear;
                if (loanTerms) data.loanTerms = loanTerms;
                if (form.notes.trim()) data.notes = form.notes.trim();
                await createAccount(data);
                showSuccess(`Created "${name}"`);
            } else {
                // accountType/liability are immutable, so they're never sent. Asset
                // classes are sent only when the type lets the user choose them.
                const updates: AccountUpdate = {
                    name,
                    owners: form.owners,
                    excludedFromNetWorth: form.excludedFromNetWorth,
                    targetYear,
                    loanTerms,
                    notes: form.notes.trim() ? form.notes.trim() : null,
                };
                if (assetClassEditable) updates.assetClasses = form.assetClasses;
                await updateAccount(editingId, updates);
                showSuccess(`Saved "${name}"`);
            }
            setTimeout(clear, 3000);
            closeForm();
            load();
        } catch (err) {
            showError(err);
        }
    };

    const handleDeactivate = async (account: Account) => {
        if (!confirm(`Deactivate "${account.name}"? Its history is kept.`)) return;
        try {
            await deactivateAccount(account.accountId);
            if (editingId === account.accountId) closeForm();
            load();
        } catch (err) {
            showError(err);
        }
    };

    const handleReactivate = async (account: Account) => {
        try {
            await reactivateAccount(account.accountId);
            load();
        } catch (err) {
            showError(err);
        }
    };

    const handleDragEnd = async (event: DragEndEvent) => {
        const { active, over } = event;
        if (!over || active.id === over.id) return;
        const oldIndex = activeAccounts.findIndex((a) => a.accountId === active.id);
        const newIndex = activeAccounts.findIndex((a) => a.accountId === over.id);
        if (oldIndex < 0 || newIndex < 0) return;
        const reordered = arrayMove(activeAccounts, oldIndex, newIndex);
        const bySort = new Map(reordered.map((a, i) => [a.accountId, i] as const));
        setAccounts((prev) =>
            prev.map((a) => {
                const idx = bySort.get(a.accountId);
                return idx === undefined ? a : { ...a, sortOrder: idx };
            }),
        );
        try {
            await reorderAccounts(reordered.map((a) => a.accountId));
        } catch (err) {
            showError(err);
            load();
        }
    };

    // --- People handlers ---

    const handleCreatePerson = async () => {
        if (!personName.trim() || !personBirth.trim()) {
            showError("Person name and birth month are required");
            return;
        }
        try {
            await createPerson(personName.trim(), personBirth.trim());
            setPersonName("");
            setPersonBirth("");
            setShowPersonForm(false);
            load();
        } catch (err) {
            showError(err);
        }
    };

    const handleSavePerson = async (personId: string) => {
        try {
            await updatePerson(personId, {
                name: editPersonName.trim(),
                birthYearMonth: editPersonBirth.trim(),
            });
            setEditingPersonId(null);
            load();
        } catch (err) {
            showError(err);
        }
    };

    const handleDeletePerson = async (person: Person) => {
        if (!confirm(`Delete "${person.name}"? This can't be undone.`)) return;
        try {
            await deletePerson(person.personId);
            load();
        } catch (err) {
            if (err instanceof ApiError && err.status === 409) {
                showError(`"${person.name}" still owns one or more accounts. Reassign those accounts first.`);
                return;
            }
            showError(err);
        }
    };

    // --- Render ---

    const renderAccountCells = (account: Account) => {
        const m = ACCOUNT_TYPE_META[account.accountType];
        return (
            <>
                <td>
                    {account.name}
                    {account.excludedFromNetWorth && (
                        <span
                            className="badge badge-pinned nw-excluded-badge"
                            title="Tracked but excluded from net worth totals"
                        >
                            Excluded
                        </span>
                    )}
                </td>
                <td>{ACCOUNT_TYPE_LABELS[account.accountType] ?? account.accountType}</td>
                {/* Asset class is meaningless for liabilities. Target Date shows its vintage. */}
                <td>
                    {account.liability
                        ? "—"
                        : account.assetClasses
                              .map((c) => {
                                  const label = ASSET_CLASS_LABELS[c] ?? c;
                                  return c === "target_date" && account.targetYear
                                      ? `${label} (${account.targetYear})`
                                      : label;
                              })
                              .join(", ")}
                </td>
                <td>{ownersLabel(account.owners)}</td>
                <td>
                    {account.liability ? (
                        <span className="badge badge-delete">Liability</span>
                    ) : (
                        <span className="badge badge-create">Asset</span>
                    )}
                </td>
                <td>
                    {m?.amortizing && account.loanTerms
                        ? `${+(account.loanTerms.interestRate * 100).toFixed(6)}% → ${formatYearMonth(account.loanTerms.payoffYearMonth)}`
                        : "—"}
                </td>
            </>
        );
    };

    const assetClassEditable = !meta.liability && meta.fixedAssetClass === null;

    return (
        <div className="page">
            <div className="section-heading">
                <h1>Accounts</h1>
                {!showAccountForm && (
                    <button className="small-btn" onClick={openCreate}>
                        + Add account
                    </button>
                )}
            </div>
            <LoadingOverlay message={status.message} visible={status.type === "loading"} />
            <StatusMessage message={status.type !== "loading" ? status.message : ""} type={status.type} />

            {/* Account create / edit form */}
            {showAccountForm && (
                <section className="account-form">
                    <h2>{editingId === null ? "Add account" : "Edit account"}</h2>

                    <div className="form-field">
                        <label htmlFor="acct-name">Name</label>
                        <input
                            id="acct-name"
                            type="text"
                            value={form.name}
                            onChange={(e) => setForm({ ...form, name: e.target.value })}
                        />
                    </div>

                    <div className="form-field">
                        <label htmlFor="acct-type">Account type</label>
                        {editingId === null ? (
                            <select
                                id="acct-type"
                                value={form.accountType}
                                onChange={(e) => changeAccountType(e.target.value)}
                            >
                                {ACCOUNT_TYPES.map((t) => (
                                    <option key={t} value={t}>
                                        {ACCOUNT_TYPE_LABELS[t]}
                                    </option>
                                ))}
                            </select>
                        ) : (
                            <span className="readonly-field">
                                {ACCOUNT_TYPE_LABELS[form.accountType] ?? form.accountType}{" "}
                                <span className="hint">(type can't be changed — deactivate & recreate instead)</span>
                            </span>
                        )}
                    </div>

                    <div className="form-field">
                        <label>Kind</label>
                        <span className="readonly-field">{meta.liability ? "Liability" : "Asset"}</span>
                    </div>

                    {/* Asset classes: hidden for liabilities, read-only when fixed, a
                        multi-select when chosen. Picking Target Date reveals a vintage
                        year dropdown. */}
                    {!meta.liability &&
                        (assetClassEditable ? (
                            <>
                                <div className="form-field">
                                    <label>Asset class(es)</label>
                                    <div className="owners-checkboxes">
                                        {ASSET_CLASSES.map((c) => (
                                            <label key={c}>
                                                <input
                                                    type="checkbox"
                                                    checked={form.assetClasses.includes(c)}
                                                    onChange={(e) => toggleAssetClass(c, e.target.checked)}
                                                />
                                                {ASSET_CLASS_LABELS[c] ?? c}
                                            </label>
                                        ))}
                                    </div>
                                </div>
                                {form.assetClasses.includes("target_date") && (
                                    <div className="form-field">
                                        <label htmlFor="acct-target-year">Target year</label>
                                        <select
                                            id="acct-target-year"
                                            value={form.targetYear ?? ""}
                                            onChange={(e) =>
                                                setForm({
                                                    ...form,
                                                    targetYear: e.target.value ? Number(e.target.value) : null,
                                                })
                                            }
                                        >
                                            <option value="">Select year…</option>
                                            {TARGET_YEAR_VINTAGES.map((y) => (
                                                <option key={y} value={y}>
                                                    {y}
                                                </option>
                                            ))}
                                        </select>
                                    </div>
                                )}
                            </>
                        ) : (
                            <div className="form-field">
                                <label>Asset class</label>
                                <span className="readonly-field">
                                    {(form.assetClasses[0] && ASSET_CLASS_LABELS[form.assetClasses[0]]) ??
                                        form.assetClasses[0]}{" "}
                                    <span className="hint">(fixed for this type)</span>
                                </span>
                            </div>
                        ))}

                    <div className="form-field">
                        <label>Owner(s)</label>
                        {people.length === 0 ? (
                            <span className="hint">Add a household member below first, then pick an owner.</span>
                        ) : (
                            <div className="owners-checkboxes">
                                {people.map((p) => (
                                    <label key={p.personId}>
                                        <input
                                            type="checkbox"
                                            checked={form.owners.includes(p.personId)}
                                            onChange={(e) => toggleOwner(p.personId, e.target.checked)}
                                        />
                                        {p.name}
                                    </label>
                                ))}
                            </div>
                        )}
                    </div>

                    {meta.amortizing && (
                        <div className="form-field">
                            <label>Loan terms (optional, all-or-nothing)</label>
                            <div className="loan-terms-fields">
                                <div className="loan-term-field">
                                    <label htmlFor="loan-rate">Interest rate (%)</label>
                                    <input
                                        id="loan-rate"
                                        type="text"
                                        placeholder="e.g. 4.875"
                                        value={form.loanRatePct}
                                        onChange={(e) => setForm({ ...form, loanRatePct: e.target.value })}
                                    />
                                </div>
                                <div className="loan-term-field">
                                    <label htmlFor="loan-payment">Monthly payment ($)</label>
                                    <input
                                        id="loan-payment"
                                        type="text"
                                        placeholder="e.g. 2500.00"
                                        value={form.loanPayment}
                                        onChange={(e) => setForm({ ...form, loanPayment: e.target.value })}
                                    />
                                </div>
                                <div className="loan-term-field">
                                    <label htmlFor="loan-payoff">Payoff month</label>
                                    <MonthYearInput
                                        id="loan-payoff"
                                        value={form.loanPayoff}
                                        onChange={(v) => setForm({ ...form, loanPayoff: v })}
                                        minYear={new Date().getFullYear()}
                                    />
                                </div>
                            </div>
                        </div>
                    )}

                    <div className="form-field">
                        <label className="checkbox-label">
                            <input
                                type="checkbox"
                                checked={form.excludedFromNetWorth}
                                onChange={(e) => setForm({ ...form, excludedFromNetWorth: e.target.checked })}
                            />
                            Exclude from net worth totals
                        </label>
                        <span className="hint">
                            Track this account's balances but leave them out of Total Assets / Liabilities / Net Worth
                            (e.g. a 529 you follow but don't own).
                        </span>
                    </div>

                    <div className="form-field">
                        <label htmlFor="acct-notes">Notes (optional)</label>
                        <input
                            id="acct-notes"
                            type="text"
                            value={form.notes}
                            onChange={(e) => setForm({ ...form, notes: e.target.value })}
                        />
                    </div>

                    <div className="form-actions">
                        <button className="primary-btn" onClick={handleSubmit}>
                            {editingId === null ? "Add account" : "Save changes"}
                        </button>
                        <button className="small-btn" onClick={closeForm}>
                            Cancel
                        </button>
                    </div>
                </section>
            )}

            {/* Active accounts */}
            <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
                <table className="data-table">
                    <thead>
                        <tr>
                            <th></th>
                            <th>Name</th>
                            <th>Type</th>
                            <th>Asset class</th>
                            <th>Owner(s)</th>
                            <th>Kind</th>
                            <th>Loan</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <SortableContext
                        items={activeAccounts.map((a) => a.accountId)}
                        strategy={verticalListSortingStrategy}
                    >
                        <tbody>
                            {activeAccounts.length === 0 && (
                                <tr>
                                    <td colSpan={8} className="empty">
                                        No accounts yet
                                    </td>
                                </tr>
                            )}
                            {activeAccounts.map((account) => (
                                <SortableAccountRow key={account.accountId} account={account}>
                                    {renderAccountCells(account)}
                                    <td className="actions">
                                        <button className="small-btn" onClick={() => startEdit(account)}>
                                            Edit
                                        </button>
                                        <button
                                            className="small-btn delete-btn"
                                            onClick={() => handleDeactivate(account)}
                                        >
                                            Deactivate
                                        </button>
                                    </td>
                                </SortableAccountRow>
                            ))}
                        </tbody>
                    </SortableContext>
                </table>
            </DndContext>

            {/* Inactive accounts */}
            {inactiveAccounts.length > 0 && (
                <section className="inactive-categories">
                    <h2>Deactivated accounts</h2>
                    <table className="data-table">
                        <thead>
                            <tr>
                                <th>Name</th>
                                <th>Type</th>
                                <th>Asset class</th>
                                <th>Owner(s)</th>
                                <th>Kind</th>
                                <th>Loan</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {inactiveAccounts.map((account) => (
                                <tr key={account.accountId} className="faded">
                                    {renderAccountCells(account)}
                                    <td className="actions">
                                        <button className="small-btn" onClick={() => handleReactivate(account)}>
                                            Reactivate
                                        </button>
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </section>
            )}

            {/* Household panel */}
            <section style={{ marginTop: "2rem" }}>
                <div className="section-heading">
                    <h2>Household</h2>
                    {!showPersonForm && (
                        <button className="small-btn" onClick={() => setShowPersonForm(true)}>
                            + Add person
                        </button>
                    )}
                </div>
                <p className="hint">
                    People who can own accounts. Birth month powers age-based rules in the future simulation feature.
                </p>

                {showPersonForm && (
                    <div className="account-form">
                        <div className="form-field">
                            <label htmlFor="person-name">Name</label>
                            <input
                                id="person-name"
                                type="text"
                                value={personName}
                                onChange={(e) => setPersonName(e.target.value)}
                            />
                        </div>
                        <div className="form-field">
                            <label htmlFor="person-birth">Birth month</label>
                            <MonthYearInput
                                id="person-birth"
                                value={personBirth}
                                onChange={setPersonBirth}
                                maxYear={new Date().getFullYear()}
                            />
                        </div>
                        <div className="form-actions">
                            <button className="primary-btn" onClick={handleCreatePerson}>
                                Add person
                            </button>
                            <button
                                className="small-btn"
                                onClick={() => {
                                    setShowPersonForm(false);
                                    setPersonName("");
                                    setPersonBirth("");
                                }}
                            >
                                Cancel
                            </button>
                        </div>
                    </div>
                )}

                <table className="data-table">
                    <thead>
                        <tr>
                            <th>Name</th>
                            <th>Birth month</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {people.length === 0 && (
                            <tr>
                                <td colSpan={3} className="empty">
                                    No people yet
                                </td>
                            </tr>
                        )}
                        {people.map((person) =>
                            editingPersonId === person.personId ? (
                                <tr key={person.personId}>
                                    <td>
                                        <input
                                            type="text"
                                            value={editPersonName}
                                            autoFocus
                                            onChange={(e) => setEditPersonName(e.target.value)}
                                        />
                                    </td>
                                    <td>
                                        <MonthYearInput
                                            value={editPersonBirth}
                                            onChange={setEditPersonBirth}
                                            maxYear={new Date().getFullYear()}
                                        />
                                    </td>
                                    <td className="actions">
                                        <button className="small-btn" onClick={() => handleSavePerson(person.personId)}>
                                            Save
                                        </button>
                                        <button className="small-btn" onClick={() => setEditingPersonId(null)}>
                                            Cancel
                                        </button>
                                    </td>
                                </tr>
                            ) : (
                                <tr key={person.personId}>
                                    <td>{person.name}</td>
                                    <td>{person.birthYearMonth}</td>
                                    <td className="actions">
                                        <button
                                            className="small-btn"
                                            onClick={() => {
                                                setEditingPersonId(person.personId);
                                                setEditPersonName(person.name);
                                                setEditPersonBirth(person.birthYearMonth);
                                            }}
                                        >
                                            Edit
                                        </button>
                                        <button
                                            className="small-btn delete-btn"
                                            onClick={() => handleDeletePerson(person)}
                                        >
                                            Delete
                                        </button>
                                    </td>
                                </tr>
                            ),
                        )}
                    </tbody>
                </table>
            </section>
        </div>
    );
}
