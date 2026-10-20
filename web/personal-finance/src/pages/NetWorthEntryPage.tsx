import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { fetchAllAccounts, fetchNetWorthMonth, saveNetWorthMonth, updateAccount } from "../api";
import LoadingOverlay from "../components/LoadingOverlay";
import MonthPicker from "../components/MonthPicker";
import NetWorthHistoryPanel from "../components/NetWorthHistoryPanel";
import StatusMessage from "../components/StatusMessage";
import { currentYearMonth, formatCurrency, formatYearMonth, parseCurrencyInput } from "../format";
import { useStatus } from "../hooks";
import {
    ACCOUNT_TYPE_LABELS,
    ACCOUNT_TYPE_META,
    ASSET_CLASS_LABELS,
    ASSET_CLASSES,
    type Account,
    type NetWorthRow,
} from "../types";

// Editable state for one (account, asset-class) cell. `raw` is the dollar string in
// the input; `touched` records whether the user edited it.
interface Entry {
    raw: string;
    touched: boolean;
}

// State key for a per-class cell. ULIDs and enum values never contain "|".
const cellKey = (accountId: string, assetClass: string): string => `${accountId}|${assetClass}`;

function stripFormat(s: string): string {
    return s.replace(/[$,\s]/g, "");
}

function formatValue(s: string): string {
    const trimmed = s.trim();
    if (trimmed === "") return "";
    const millionths = parseCurrencyInput(trimmed);
    if (millionths === null) return trimmed;
    return formatCurrency(millionths);
}

// Asset classes offerable via the record pane's "+ add asset class" (excludes
// target_date, which needs a vintage year set on the Accounts page).
const ADDABLE_CLASSES = ASSET_CLASSES.filter((c) => c !== "target_date");

// When the form has more than this many asset-class input lines it's "long" enough
// that a second Save button at the bottom is worth it (so you don't scroll back up).
// Deliberately generous — a handful of accounts keeps the single top button.
const LONG_FORM_LINE_THRESHOLD = 8;

export default function NetWorthEntryPage() {
    const [yearMonth, setYearMonth] = useState<string>(currentYearMonth());
    const [rows, setRows] = useState<NetWorthRow[]>([]);
    const [accountsById, setAccountsById] = useState<Map<string, Account>>(new Map());
    const [entries, setEntries] = useState<Record<string, Entry>>({});
    // Per-account note for this month (month-specific; not carried forward).
    const [notes, setNotes] = useState<Record<string, string>>({});
    const [openNote, setOpenNote] = useState<string | null>(null);
    // Which account's "+ add asset class" picker is open.
    const [addingClassFor, setAddingClassFor] = useState<string | null>(null);
    const [historyReloadKey, setHistoryReloadKey] = useState(0);
    const { status, showLoading, showError, showSuccess, clear } = useStatus();

    const isFuture = yearMonth > currentYearMonth();

    const load = useCallback(async () => {
        if (yearMonth > currentYearMonth()) {
            setRows([]);
            setEntries({});
            clear();
            return;
        }
        showLoading("Loading...");
        try {
            const [month, allAccounts] = await Promise.all([fetchNetWorthMonth(yearMonth), fetchAllAccounts()]);
            setAccountsById(new Map(allAccounts.map((a) => [a.accountId, a])));
            setRows(month.rows);

            // Seed each per-class cell: a saved value shows as itself (asserted); a
            // carried prefill shows muted until touched; otherwise blank.
            const seeded: Record<string, Entry> = {};
            for (const row of month.rows) {
                for (const ce of row.classes) {
                    const key = cellKey(row.accountId, ce.assetClass);
                    if (ce.value !== null) {
                        seeded[key] = { raw: formatCurrency(ce.value), touched: true };
                    } else if (ce.prefill) {
                        seeded[key] = { raw: formatCurrency(ce.prefill.value), touched: false };
                    } else {
                        seeded[key] = { raw: "", touched: false };
                    }
                }
            }
            setEntries(seeded);

            const noteSeed: Record<string, string> = {};
            for (const row of month.rows) noteSeed[row.accountId] = row.note ?? "";
            setNotes(noteSeed);
            setOpenNote(null);
            setAddingClassFor(null);
            clear();
        } catch (err) {
            showError(err);
        }
    }, [yearMonth]); // eslint-disable-line react-hooks/exhaustive-deps

    useEffect(() => {
        load();
    }, [load]);

    const setRaw = (key: string, raw: string) => {
        setEntries((prev) => ({ ...prev, [key]: { raw, touched: true } }));
    };

    // Focus/blur reformatting must NOT flip `touched`.
    const reformatRaw = (key: string, raw: string) => {
        setEntries((prev) => ({ ...prev, [key]: { raw, touched: prev[key]?.touched ?? false } }));
    };

    const setNote = (accountId: string, text: string) => {
        setNotes((prev) => ({ ...prev, [accountId]: text }));
    };

    // Effective displayed value for a cell (parsed; blank/invalid → 0 for totals).
    const cellValue = (accountId: string, assetClass: string): number => {
        const raw = (entries[cellKey(accountId, assetClass)]?.raw ?? "").trim();
        if (raw === "") return 0;
        const v = parseCurrencyInput(raw);
        return v === null || v < 0 ? 0 : v;
    };

    const accountSubtotal = (row: NetWorthRow): number =>
        row.classes.reduce((sum, ce) => sum + cellValue(row.accountId, ce.assetClass), 0);

    const { assetRows, liabilityRows } = useMemo(() => {
        const assetRows: NetWorthRow[] = [];
        const liabilityRows: NetWorthRow[] = [];
        for (const row of rows) {
            if (accountsById.get(row.accountId)?.liability) liabilityRows.push(row);
            else assetRows.push(row);
        }
        return { assetRows, liabilityRows };
    }, [rows, accountsById]);

    // Live totals: each account rolls up its per-class values; excluded accounts are
    // recorded but never counted (mirrors the server).
    const totals = useMemo(() => {
        let assets = 0;
        let liabilities = 0;
        for (const row of rows) {
            const account = accountsById.get(row.accountId);
            if (account?.excludedFromNetWorth) continue;
            const subtotal = row.classes.reduce((sum, ce) => sum + cellValue(row.accountId, ce.assetClass), 0);
            if (account?.liability) liabilities += subtotal;
            else assets += subtotal;
        }
        return { assets, liabilities, netWorth: assets - liabilities };
    }, [rows, entries, accountsById]); // eslint-disable-line react-hooks/exhaustive-deps

    // Show a bottom Save button once the form is tall (many accounts, or one account
    // with many classes). One asset-class line per row, summed across all accounts.
    const showBottomSave = rows.reduce((n, r) => n + r.classes.length, 0) > LONG_FORM_LINE_THRESHOLD;

    const handleSave = async () => {
        // Save records the whole visible column, per class. Blanking a class clears
        // it; an unchanged class is a no-op. A row is sent when any class changed or
        // the note changed.
        const payload: {
            accountId: string;
            note?: string | null;
            classes: { assetClass: string; value: number | null }[];
        }[] = [];

        for (const row of rows) {
            const account = accountsById.get(row.accountId);
            const changes: { assetClass: string; value: number | null }[] = [];
            for (const ce of row.classes) {
                const raw = (entries[cellKey(row.accountId, ce.assetClass)]?.raw ?? "").trim();
                if (raw === "") {
                    if (ce.value !== null) changes.push({ assetClass: ce.assetClass, value: null }); // clear
                    continue;
                }
                const v = parseCurrencyInput(raw);
                if (v === null || v < 0) {
                    const label = ASSET_CLASS_LABELS[ce.assetClass] ?? ce.assetClass;
                    showError(`"${account?.name ?? row.accountId}" — ${label} has an invalid amount`);
                    return;
                }
                if (v === ce.value) continue; // unchanged
                changes.push({ assetClass: ce.assetClass, value: v });
            }

            const note = (notes[row.accountId] ?? "").trim();
            const noteChanged = note !== (row.note ?? "").trim();
            if (changes.length === 0 && !noteChanged) continue;
            payload.push({ accountId: row.accountId, classes: changes, note: note ? note : null });
        }

        if (payload.length === 0) {
            showSuccess("Nothing to save — no values were changed");
            setTimeout(clear, 3000);
            return;
        }

        showLoading("Saving...");
        try {
            await saveNetWorthMonth(yearMonth, payload);
            showSuccess(`Saved ${formatYearMonth(yearMonth)}`);
            setTimeout(clear, 3000);
            load();
            setHistoryReloadKey((k) => k + 1);
        } catch (err) {
            showError(err);
        }
    };

    // Add a class to an account from the record pane: inject a blank line optimistically
    // (so in-progress edits survive) and persist the account's expanded class set.
    const handleAddClass = async (accountId: string, assetClass: string) => {
        const account = accountsById.get(accountId);
        if (!account || account.assetClasses.includes(assetClass)) {
            setAddingClassFor(null);
            return;
        }
        const nextClasses = [...account.assetClasses, assetClass];
        setRows((prev) =>
            prev.map((r) =>
                r.accountId === accountId
                    ? { ...r, classes: [...r.classes, { assetClass, value: null, prefill: null }] }
                    : r,
            ),
        );
        setAccountsById((prev) => {
            const next = new Map(prev);
            next.set(accountId, { ...account, assetClasses: nextClasses });
            return next;
        });
        setAddingClassFor(null);
        try {
            await updateAccount(accountId, { assetClasses: nextClasses });
        } catch (err) {
            showError(err);
            load();
        }
    };

    // Render the rows for one account: one line per asset class, an optional
    // "+ add asset class" line, and (for multi-class accounts) a subtotal line.
    const renderAccountRows = (row: NetWorthRow) => {
        const account = accountsById.get(row.accountId);
        const multi = row.classes.length > 1;
        const editable =
            account !== undefined &&
            !account.liability &&
            ACCOUNT_TYPE_META[account.accountType]?.fixedAssetClass === null;
        const hasNote = (notes[row.accountId] ?? "").trim() !== "";
        const addable = ADDABLE_CLASSES.filter((c) => !row.classes.some((ce) => ce.assetClass === c));

        return (
            <Fragment key={row.accountId}>
                {row.classes.map((ce, i) => {
                    const key = cellKey(row.accountId, ce.assetClass);
                    const entry = entries[key] ?? { raw: "", touched: false };
                    const isCarried = ce.value === null && ce.prefill !== null && !entry.touched;
                    const classLabel =
                        ce.assetClass === "target_date" && account?.targetYear
                            ? `${ASSET_CLASS_LABELS[ce.assetClass]} (${account.targetYear})`
                            : (ASSET_CLASS_LABELS[ce.assetClass] ?? ce.assetClass);
                    return (
                        <tr key={key} className={isCarried ? "faded" : ""}>
                            <td>
                                {i === 0 && (
                                    <>
                                        <div>
                                            {account?.name ?? row.accountId}
                                            {account?.excludedFromNetWorth && (
                                                <span
                                                    className="badge badge-pinned nw-excluded-badge"
                                                    title="Excluded from net worth totals"
                                                >
                                                    Excluded
                                                </span>
                                            )}
                                        </div>
                                        {account && (
                                            <div className="nw-entry-type">
                                                {ACCOUNT_TYPE_LABELS[account.accountType] ?? account.accountType}
                                            </div>
                                        )}
                                    </>
                                )}
                            </td>
                            <td className="nw-class-cell">{classLabel}</td>
                            <td className="num">
                                <div className="nw-value-row">
                                    <input
                                        type="text"
                                        inputMode="decimal"
                                        className="currency-input"
                                        value={entry.raw}
                                        onChange={(e) => setRaw(key, e.target.value)}
                                        onFocus={(e) => reformatRaw(key, stripFormat(e.target.value))}
                                        onBlur={(e) => reformatRaw(key, formatValue(e.target.value))}
                                    />
                                    {/* Note toggle sits on the first class row (notes are per account). */}
                                    {i === 0 && (
                                        <button
                                            type="button"
                                            className={`nw-note-toggle${hasNote ? " has-note" : ""}`}
                                            title={hasNote ? (notes[row.accountId] ?? "") : "Add note"}
                                            aria-label={hasNote ? "Edit note" : "Add note"}
                                            onClick={() =>
                                                setOpenNote((cur) => (cur === row.accountId ? null : row.accountId))
                                            }
                                        >
                                            {hasNote ? "●" : "○"}
                                        </button>
                                    )}
                                </div>
                                {isCarried && ce.prefill && (
                                    <div className="nw-carried-note">
                                        carried from {formatYearMonth(ce.prefill.fromYearMonth)}
                                    </div>
                                )}
                                {i === 0 && openNote === row.accountId && (
                                    <textarea
                                        className="nw-note-editor"
                                        rows={2}
                                        placeholder="Note for this month"
                                        value={notes[row.accountId] ?? ""}
                                        onChange={(e) => setNote(row.accountId, e.target.value)}
                                    />
                                )}
                            </td>
                        </tr>
                    );
                })}

                {editable && addable.length > 0 && (
                    <tr className="nw-add-class-row">
                        <td></td>
                        <td colSpan={2}>
                            {addingClassFor === row.accountId ? (
                                <select
                                    autoFocus
                                    defaultValue=""
                                    onChange={(e) => e.target.value && handleAddClass(row.accountId, e.target.value)}
                                >
                                    <option value="">Add asset class…</option>
                                    {addable.map((c) => (
                                        <option key={c} value={c}>
                                            {ASSET_CLASS_LABELS[c] ?? c}
                                        </option>
                                    ))}
                                </select>
                            ) : (
                                <button className="small-btn" onClick={() => setAddingClassFor(row.accountId)}>
                                    + add asset class
                                </button>
                            )}
                        </td>
                    </tr>
                )}

                {multi && (
                    <tr className="nw-subtotal-row">
                        <td></td>
                        <td>Subtotal</td>
                        <td className="num">{formatCurrency(accountSubtotal(row))}</td>
                    </tr>
                )}
            </Fragment>
        );
    };

    const renderTable = (title: string, sectionRows: NetWorthRow[], total: number) => (
        <section>
            <h2>{title}</h2>
            <table className="data-table networth-grid">
                <colgroup>
                    <col className="nw-col-account" />
                    <col className="nw-col-class" />
                    <col className="nw-col-value" />
                </colgroup>
                <thead>
                    <tr>
                        <th>Account</th>
                        <th>Asset class</th>
                        <th>Value</th>
                    </tr>
                </thead>
                <tbody>
                    {sectionRows.map(renderAccountRows)}
                    <tr className="nw-total-row">
                        <td colSpan={2}>Total {title}</td>
                        <td className="num">{formatCurrency(total)}</td>
                    </tr>
                </tbody>
            </table>
        </section>
    );

    return (
        <div className="page">
            <h1>Household Net Worth</h1>
            <LoadingOverlay message={status.message} visible={status.type === "loading"} />
            <StatusMessage message={status.type !== "loading" ? status.message : ""} type={status.type} />

            <div className="networth-split">
                {/* Left pane: record this month's values. */}
                <section className="networth-pane networth-entry-pane">
                    <h2 className="pane-title">Record</h2>
                    <div className="toolbar">
                        <MonthPicker
                            value={yearMonth}
                            onChange={setYearMonth}
                            showBadges={false}
                            maxMonth={currentYearMonth()}
                        />
                    </div>

                    {isFuture ? (
                        <p className="hint">
                            Future months can't be recorded — net worth is entered as it happens. Pick the current month
                            or a past one.
                        </p>
                    ) : rows.length === 0 ? (
                        <p className="empty">
                            No accounts to record. Add accounts on the <strong>Accounts</strong> page first.
                        </p>
                    ) : (
                        <>
                            <div className="toolbar">
                                <button className="primary-btn" onClick={handleSave}>
                                    Save {formatYearMonth(yearMonth)}
                                </button>
                            </div>

                            {assetRows.length > 0 && renderTable("Assets", assetRows, totals.assets)}
                            {liabilityRows.length > 0 && renderTable("Liabilities", liabilityRows, totals.liabilities)}

                            <table className="data-table networth-grid nw-networth-summary">
                                <colgroup>
                                    <col className="nw-col-account" />
                                    <col className="nw-col-class" />
                                    <col className="nw-col-value" />
                                </colgroup>
                                <tbody>
                                    <tr className="nw-total-row nw-networth-row">
                                        <td colSpan={2}>Net Worth</td>
                                        <td className="num">{formatCurrency(totals.netWorth)}</td>
                                    </tr>
                                </tbody>
                            </table>

                            {/* Second Save button for long forms — same behavior as the top one. */}
                            {showBottomSave && (
                                <div className="toolbar nw-bottom-save">
                                    <button className="primary-btn" onClick={handleSave}>
                                        Save {formatYearMonth(yearMonth)}
                                    </button>
                                </div>
                            )}
                        </>
                    )}
                </section>

                {/* Right pane: historical values + net-worth-over-time chart. */}
                <section className="networth-pane networth-history-pane">
                    <NetWorthHistoryPanel
                        reloadKey={historyReloadKey}
                        selectedMonth={yearMonth}
                        onSelectMonth={setYearMonth}
                    />
                </section>
            </div>
        </div>
    );
}
