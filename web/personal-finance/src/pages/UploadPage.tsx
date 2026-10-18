import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { useAdmin } from "../admin";
import { ApiError, commitTransactions, fetchAccounts, fetchCategories, uploadTransactions } from "../api";
import LoadingOverlay from "../components/LoadingOverlay";
import StatusMessage from "../components/StatusMessage";
import { formatCurrency } from "../format";
import { useStatus } from "../hooks";
import { parseCommitValidationsFromError } from "../parsers";
import {
    BUDGET_ROLE_LABELS,
    DISPOSITION_LABELS,
    DISPOSITIONS,
    REVIEW_REASON_LABELS,
    budgetRoleOf,
    type Account,
    type Category,
    type Disposition,
    type ParsedTransaction,
    type RowValidation,
    type RowValidationIssue,
    type SignCheck,
    type SuggestedBy,
} from "../types";

type SortKey = "transactionDate" | "description" | "amount" | "categoryName";
type SortDir = "asc" | "desc";

const SUGGESTED_BY_LABELS: Record<SuggestedBy, string> = {
    PAIR: "matched the other side of a transfer",
    LAST_FOUR: "found an account number",
    BANK: "from the bank's category",
    LLM: "from the LLM",
    DEFAULT: "default",
};

function yearMonthFromDate(dateStr: string): string {
    return dateStr.slice(0, 7);
}

function issueLabel(issue: RowValidationIssue): string {
    switch (issue) {
        case "missing_category":
            return "Pick a category";
        case "locked_month":
            return "Locked month (override required)";
    }
}

export default function UploadPage() {
    const { adminModeOn } = useAdmin();
    const [csvFile, setCsvFile] = useState<File | null>(null);
    const [accounts, setAccounts] = useState<Account[]>([]);
    const [accountId, setAccountId] = useState("");
    const [transactions, setTransactions] = useState<ParsedTransaction[]>([]);
    const [validations, setValidations] = useState<RowValidation[]>([]);
    // The rows that the commit leaves out. A row that the table holds already starts here.
    const [removed, setRemoved] = useState<Set<number>>(new Set());
    const [categories, setCategories] = useState<Category[]>([]);
    const [signCheck, setSignCheck] = useState<SignCheck | null>(null);
    const { status, showLoading, showSuccess, showError, clear } = useStatus();
    const [step, setStep] = useState<"upload" | "review" | "done">("upload");

    const [sortKey, setSortKey] = useState<SortKey>("transactionDate");
    const [sortDir, setSortDir] = useState<SortDir>("asc");

    // Only an account with a budget role can take an upload.
    const uploadAccounts = useMemo(() => accounts.filter((a) => budgetRoleOf(a) !== null), [accounts]);

    useEffect(() => {
        fetchAccounts()
            .then(setAccounts)
            .catch((err: unknown) => showError(err));
    }, []); // eslint-disable-line react-hooks/exhaustive-deps

    // `flipSigns` sends the file again with every sign flipped. The server must do the work
    // again, because the fingerprint of a row holds its amount.
    const runUpload = useCallback(
        async (flipSigns: boolean) => {
            if (!csvFile || !accountId) return;
            showLoading("Uploading and analyzing...");
            try {
                const text = await csvFile.text();
                const [result, cats] = await Promise.all([
                    uploadTransactions(text, accountId, flipSigns),
                    fetchCategories(),
                ]);
                setSignCheck(result.signCheck);
                setTransactions(result.transactions);
                setValidations(result.validations);
                setRemoved(new Set(result.transactions.flatMap((t, i) => (t.duplicate ? [i] : []))));
                setCategories(cats);
                setStep("review");
                const duplicates = result.transactions.filter((t) => t.duplicate).length;
                showSuccess(
                    `Found ${result.transactions.length} transactions` +
                        (duplicates > 0 ? `; ${duplicates} exist already and are not selected` : ""),
                );
                setTimeout(clear, 5000);
            } catch (err) {
                showError(err);
            }
        },
        [csvFile, accountId], // eslint-disable-line react-hooks/exhaustive-deps
    );
    const handleUpload = () => runUpload(false);
    const handleFlipSigns = () => runUpload(!(signCheck?.userFlipped ?? false));

    const reviewByIndex = useMemo(() => new Map(validations.map((v) => [v.index, v.review])), [validations]);

    // Validations come back keyed by row index. Editing a row's category or removing
    // a row can resolve issues client-side, so we recompute the displayed status from
    // the latest state rather than relying on the server's original list.
    const liveIssues = useMemo<Map<number, RowValidationIssue[]>>(() => {
        const m = new Map<number, RowValidationIssue[]>();
        transactions.forEach((txn, index) => {
            const issues: RowValidationIssue[] = [];
            // A Budget row needs a category. The other dispositions do not.
            if (txn.disposition === "BUDGET" && !txn.categoryId) issues.push("missing_category");
            const serverIssues = validations.find((v) => v.index === index)?.issues ?? [];
            // locked_month is server-decided; we don't recompute it because
            // the client doesn't know the exact editability state machine.
            // Admin mode optimistically suppresses it — backend re-validates.
            if (serverIssues.includes("locked_month") && !adminModeOn) issues.push("locked_month");
            if (issues.length) m.set(index, issues);
        });
        return m;
    }, [validations, transactions, adminModeOn]);

    const updateTxn = (index: number, change: Partial<ParsedTransaction>) => {
        const updated = [...transactions];
        updated[index] = { ...updated[index], ...change };
        if (change.categoryId !== undefined) {
            const cat = categories.find((c) => c.categoryId === change.categoryId);
            updated[index].categoryName = cat?.name ?? "";
        }
        setTransactions(updated);
    };

    const toggleRemove = (index: number) => {
        setRemoved((prev) => {
            const next = new Set(prev);
            if (next.has(index)) next.delete(index);
            else next.add(index);
            return next;
        });
    };

    const activeRowsWithIndex = useMemo(
        () => transactions.map((t, i) => ({ ...t, _i: i })).filter((t) => !removed.has(t._i)),
        [transactions, removed],
    );
    const activeCount = activeRowsWithIndex.length;

    const blockingIssueCount = useMemo(
        () => activeRowsWithIndex.filter((t) => (liveIssues.get(t._i) ?? []).length > 0).length,
        [activeRowsWithIndex, liveIssues],
    );
    const canCommit = activeCount > 0 && blockingIssueCount === 0;

    const handleCommit = async () => {
        showLoading("Saving transactions...");
        try {
            const rows = activeRowsWithIndex.map((t) => ({
                transactionDate: t.transactionDate,
                description: t.description,
                amount: t.amount,
                disposition: t.disposition,
                ...(t.disposition !== "SKIP" && t.categoryId ? { categoryId: t.categoryId } : {}),
                accountId: t.accountId,
                ...(t.note?.trim() ? { note: t.note.trim() } : {}),
                fingerprint: t.fingerprint,
            }));
            const result = await commitTransactions(rows, adminModeOn);
            setStep("done");
            const skipped = result.skippedDuplicates.length;
            showSuccess(
                `Saved ${result.count} transactions` +
                    (skipped > 0 ? `; skipped ${skipped} that the table holds already` : ""),
            );
        } catch (err) {
            // Server returned 409 with full validation list — surface it back into the UI.
            if (err instanceof ApiError && err.status === 409) {
                const v = parseCommitValidationsFromError(err.responseBody);
                if (v) {
                    // Server validations are keyed by index into the committed batch (filtered).
                    // Re-key to the original transaction indices, and keep the review reasons.
                    const originalIndices = activeRowsWithIndex.map((t) => t._i);
                    const remappedValidations: RowValidation[] = v.map((rv) => {
                        const index = originalIndices[rv.index] ?? rv.index;
                        return { index, issues: rv.issues, review: reviewByIndex.get(index) ?? [] };
                    });
                    setValidations(remappedValidations);
                    showError("Commit rejected — fix the highlighted rows and try again.");
                    return;
                }
            }
            showError(err);
        }
    };

    const reset = () => {
        setCsvFile(null);
        setTransactions([]);
        setValidations([]);
        setRemoved(new Set());
        setStep("upload");
        clear();
    };

    const sortedIndices = useMemo(() => {
        const indices = transactions.map((_, i) => i);
        indices.sort((a, b) => {
            const ta = transactions[a];
            const tb = transactions[b];
            let cmp = 0;
            switch (sortKey) {
                case "transactionDate":
                    cmp = ta.transactionDate.localeCompare(tb.transactionDate);
                    break;
                case "description":
                    cmp = ta.description.localeCompare(tb.description);
                    break;
                case "amount":
                    cmp = ta.amount - tb.amount;
                    break;
                case "categoryName":
                    cmp = ta.categoryName.localeCompare(tb.categoryName);
                    break;
            }
            return sortDir === "asc" ? cmp : -cmp;
        });
        return indices;
    }, [transactions, sortKey, sortDir]);

    // Three lists: the rows to look at first, then the Budget rows, then the Off-budget and Skip
    // rows. A row moves to another list when the user changes its disposition.
    const sections = useMemo(() => {
        const needsReview = sortedIndices.filter((i) => (reviewByIndex.get(i) ?? []).length > 0);
        const rest = sortedIndices.filter((i) => (reviewByIndex.get(i) ?? []).length === 0);
        return [
            { title: "Look at these first", indices: needsReview },
            { title: "Budget", indices: rest.filter((i) => transactions[i].disposition === "BUDGET") },
            { title: "Off-budget and Skip", indices: rest.filter((i) => transactions[i].disposition !== "BUDGET") },
        ].filter((s) => s.indices.length > 0);
    }, [sortedIndices, reviewByIndex, transactions]);

    const handleSort = (key: SortKey) => {
        if (sortKey === key) {
            setSortDir(sortDir === "asc" ? "desc" : "asc");
        } else {
            setSortKey(key);
            setSortDir("asc");
        }
    };

    const sortIndicator = (key: SortKey) => {
        if (sortKey !== key) return " ↕";
        return sortDir === "asc" ? " ↑" : " ↓";
    };

    const renderRow = (i: number) => {
        const txn = transactions[i];
        const isRemoved = removed.has(i);
        const issues = liveIssues.get(i) ?? [];
        const review = reviewByIndex.get(i) ?? [];
        const negative = txn.amount < 0;
        return (
            <tr
                key={i}
                className={[isRemoved ? "removed-row" : "", issues.length > 0 ? "row-has-issue" : ""].join(" ")}
            >
                <td>{txn.transactionDate}</td>
                <td>{txn.description}</td>
                <td className={`num ${negative ? "amount-refund" : ""}`}>
                    {formatCurrency(txn.amount)}
                    {negative && <span className="badge badge-refund">money in</span>}
                </td>
                <td>
                    <select
                        value={txn.disposition}
                        onChange={(e) => updateTxn(i, { disposition: e.target.value as Disposition })}
                        title={`Suggestion ${SUGGESTED_BY_LABELS[txn.suggestedBy]}`}
                    >
                        {DISPOSITIONS.map((d) => (
                            <option key={d} value={d}>
                                {DISPOSITION_LABELS[d]}
                            </option>
                        ))}
                    </select>
                </td>
                <td>
                    {txn.disposition !== "SKIP" && (
                        <select value={txn.categoryId} onChange={(e) => updateTxn(i, { categoryId: e.target.value })}>
                            <option value="">{txn.disposition === "BUDGET" ? "Uncategorized" : "No category"}</option>
                            {categories.map((c) => (
                                <option key={c.categoryId} value={c.categoryId}>
                                    {c.name}
                                </option>
                            ))}
                        </select>
                    )}
                </td>
                <td>
                    <input
                        type="text"
                        maxLength={200}
                        value={txn.note ?? ""}
                        placeholder="Note"
                        onChange={(e) => updateTxn(i, { note: e.target.value })}
                    />
                </td>
                <td className="row-issues">
                    {txn.duplicate && (
                        <span className="badge badge-duplicate" title="The table holds this row already">
                            duplicate
                        </span>
                    )}
                    {review.map((reason) => (
                        <span key={reason} className="badge badge-review">
                            {REVIEW_REASON_LABELS[reason]}
                        </span>
                    ))}
                    {issues.map((issue) => (
                        <span
                            key={issue}
                            className={`badge badge-issue badge-issue-${issue}`}
                            title={`${issue} (${yearMonthFromDate(txn.transactionDate)})`}
                        >
                            {issueLabel(issue)}
                        </span>
                    ))}
                </td>
                <td className="actions">
                    <button className={`small-btn ${isRemoved ? "" : "delete-btn"}`} onClick={() => toggleRemove(i)}>
                        {isRemoved ? "Restore" : "Remove"}
                    </button>
                </td>
            </tr>
        );
    };

    return (
        <div className="page">
            <h1>Upload Transactions</h1>
            <LoadingOverlay message={status.message} visible={status.type === "loading"} />
            <StatusMessage message={status.type !== "loading" ? status.message : ""} type={status.type} />

            {step === "upload" && (
                <div className="upload-section">
                    <p className="hint">Upload a CSV from your bank or credit card statement.</p>
                    <div className="upload-controls">
                        <select value={accountId} onChange={(e) => setAccountId(e.target.value)}>
                            <option value="">Select the account of this CSV...</option>
                            {uploadAccounts.map((a) => {
                                const role = budgetRoleOf(a);
                                return (
                                    <option key={a.accountId} value={a.accountId}>
                                        {a.name}
                                        {role ? ` — ${BUDGET_ROLE_LABELS[role]}` : ""}
                                    </option>
                                );
                            })}
                        </select>
                        <input type="file" accept=".csv" onChange={(e) => setCsvFile(e.target.files?.[0] ?? null)} />
                        <button className="primary-btn" onClick={handleUpload} disabled={!csvFile || !accountId}>
                            Upload & Analyze
                        </button>
                    </div>
                    {uploadAccounts.length === 0 && (
                        <p className="hint">
                            No account can take an upload yet. On the Accounts page, add an account and give it a budget
                            role.
                        </p>
                    )}
                </div>
            )}

            {step === "review" && (
                <>
                    <p className="hint">
                        Review the rows below. Change the type, the category, or the note, remove rows, then commit.
                        Off-budget rows count only in the Off-budget total. Skip rows count nowhere.
                        {adminModeOn && (
                            <span className="hint-admin">
                                {" "}
                                Admin mode: locked-month rows will commit with override.
                            </span>
                        )}
                    </p>
                    <p className="hint">
                        Money out shows as a positive amount. If the signs look wrong, flip them.
                        {signCheck?.locallyFlipped &&
                            " (The app flipped them already, because most rows of this spending account came in as" +
                                " money in.)"}{" "}
                        <button className="small-btn" onClick={handleFlipSigns}>
                            Flip all signs
                        </button>
                    </p>
                    {blockingIssueCount > 0 && (
                        <p className="hint warn">
                            {blockingIssueCount} row{blockingIssueCount !== 1 ? "s" : ""} need attention before
                            committing.
                        </p>
                    )}
                    <table className="data-table">
                        <thead>
                            <tr>
                                <th className="sortable" onClick={() => handleSort("transactionDate")}>
                                    Date{sortIndicator("transactionDate")}
                                </th>
                                <th className="sortable" onClick={() => handleSort("description")}>
                                    Description{sortIndicator("description")}
                                </th>
                                <th className="sortable num" onClick={() => handleSort("amount")}>
                                    Amount{sortIndicator("amount")}
                                </th>
                                <th>Type</th>
                                <th className="sortable" onClick={() => handleSort("categoryName")}>
                                    Category{sortIndicator("categoryName")}
                                </th>
                                <th>Note</th>
                                <th>Status</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {sections.map((section) => (
                                <SectionRows key={section.title} title={section.title}>
                                    {section.indices.map(renderRow)}
                                </SectionRows>
                            ))}
                        </tbody>
                    </table>
                    <div className="toolbar">
                        <button className="primary-btn" onClick={handleCommit} disabled={!canCommit}>
                            Commit {activeCount} Transaction{activeCount !== 1 ? "s" : ""}
                            {adminModeOn ? " (with override)" : ""}
                        </button>
                        <button className="secondary-btn" onClick={reset}>
                            Start Over
                        </button>
                    </div>
                </>
            )}

            {step === "done" && (
                <div className="done-section">
                    <button className="primary-btn" onClick={reset}>
                        Upload More
                    </button>
                </div>
            )}
        </div>
    );
}

function SectionRows({ title, children }: { title: string; children: ReactNode }) {
    return (
        <>
            <tr className="summary-group-header">
                <td colSpan={8}>{title}</td>
            </tr>
            {children}
        </>
    );
}
