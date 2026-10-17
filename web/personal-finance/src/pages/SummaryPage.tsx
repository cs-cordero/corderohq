import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { fetchAllCategories, fetchCategoryGroups, fetchSummary } from "../api";
import MonthPicker from "../components/MonthPicker";
import LoadingOverlay from "../components/LoadingOverlay";
import StatusMessage from "../components/StatusMessage";
import { currentYearMonth, formatCurrency } from "../format";
import { useStatus } from "../hooks";
import type { Category, CategoryGroup, Summary, SummaryCategory } from "../types";

export default function SummaryPage() {
    const [month, setMonth] = useState(currentYearMonth);
    const [summary, setSummary] = useState<Summary | null>(null);
    const [categories, setCategories] = useState<Category[]>([]);
    const [groups, setGroups] = useState<CategoryGroup[]>([]);
    const { status, showLoading, showError, clear } = useStatus();

    const load = useCallback(async () => {
        showLoading("Loading...");
        try {
            const [s, cats, grps] = await Promise.all([
                fetchSummary(month),
                fetchAllCategories(),
                fetchCategoryGroups(),
            ]);
            setSummary(s);
            setCategories(cats);
            setGroups(grps);
            clear();
        } catch (err) {
            showError(err);
        }
    }, [month]); // eslint-disable-line react-hooks/exhaustive-deps

    useEffect(() => {
        load();
    }, [load]);

    const renderName = (cat: SummaryCategory) => {
        if (cat.historicalName) return `${cat.historicalName} (renamed to: ${cat.name})`;
        const live = categories.find((c) => c.categoryId === cat.categoryId);
        if (live && !live.active) return `${cat.name} (deactivated)`;
        return cat.name;
    };

    // Group the summary's category rows by their groupId (resolved from the live
    // Category list, since the summary endpoint doesn't return the group). Rows
    // for unknown / deactivated-group categories fall into a synthetic
    // "Ungrouped" bucket displayed last.
    const grouped = useMemo(() => {
        if (!summary) return [];
        const catById = new Map(categories.map((c) => [c.categoryId, c]));
        const sortedGroups = [...groups].sort((a, b) => a.order - b.order);
        const buckets = new Map<string, { group: CategoryGroup | null; rows: SummaryCategory[] }>();
        for (const g of sortedGroups) {
            buckets.set(g.groupId, { group: g, rows: [] });
        }
        buckets.set("", { group: null, rows: [] });

        for (const row of summary.categories) {
            const live = catById.get(row.categoryId);
            const gid = live?.groupId ?? "";
            const bucket = buckets.get(gid) ?? buckets.get("");
            bucket?.rows.push(row);
        }
        for (const bucket of buckets.values()) {
            bucket.rows.sort((a, b) => {
                const la = catById.get(a.categoryId);
                const lb = catById.get(b.categoryId);
                const oa = la?.orderInGroup ?? Infinity;
                const ob = lb?.orderInGroup ?? Infinity;
                return oa - ob || a.name.localeCompare(b.name);
            });
        }
        return Array.from(buckets.values()).filter((b) => b.rows.length > 0);
    }, [summary, categories, groups]);

    return (
        <div className="page">
            <h1>Budget Summary</h1>
            <LoadingOverlay message={status.message} visible={status.type === "loading"} />
            <MonthPicker value={month} onChange={setMonth} />
            <StatusMessage message={status.type !== "loading" ? status.message : ""} type={status.type} />

            {summary && (
                <table className="data-table">
                    <thead>
                        <tr>
                            <th>Category</th>
                            <th className="num">Budgeted</th>
                            <th className="num">Actual</th>
                            <th className="num">Remaining</th>
                        </tr>
                    </thead>
                    <tbody>
                        {grouped.map(({ group, rows }) => (
                            <Fragment key={group?.groupId ?? "ungrouped"}>
                                <tr className="summary-group-header">
                                    <td colSpan={4}>{group ? group.name : "Ungrouped"}</td>
                                </tr>
                                {rows.map((cat) => (
                                    <tr key={cat.categoryId} className={cat.delta < 0 ? "over-budget" : ""}>
                                        <td>{renderName(cat)}</td>
                                        <td className="num">{formatCurrency(cat.budgeted)}</td>
                                        <td className={`num ${cat.actual < 0 ? "amount-refund" : ""}`}>
                                            {formatCurrency(cat.actual)}
                                        </td>
                                        <td className="num">{formatCurrency(cat.delta)}</td>
                                    </tr>
                                ))}
                            </Fragment>
                        ))}
                    </tbody>
                    <tfoot>
                        <tr>
                            <td>
                                <strong>Total</strong>
                            </td>
                            <td className="num">
                                <strong>{formatCurrency(summary.totals.budgeted)}</strong>
                            </td>
                            <td className="num">
                                <strong>{formatCurrency(summary.totals.actual)}</strong>
                            </td>
                            <td className="num">
                                <strong>{formatCurrency(summary.totals.delta)}</strong>
                            </td>
                        </tr>
                        {/* Off-budget money is not in the Total above, so the Total delta shows no
                            false overspend. The last row adds it back, to show all money spent. */}
                        <tr className="summary-off-budget-row">
                            <td>Off-budget</td>
                            <td className="num">{formatCurrency(0)}</td>
                            <td className="num">{formatCurrency(summary.offBudget.actual)}</td>
                            <td className="num">{"\u2014"}</td>
                        </tr>
                        <tr className="summary-off-budget-row">
                            <td>Total + off-budget</td>
                            <td className="num">{formatCurrency(summary.totalsWithOffBudget.budgeted)}</td>
                            <td className="num">{formatCurrency(summary.totalsWithOffBudget.actual)}</td>
                            <td className="num">{"\u2014"}</td>
                        </tr>
                    </tfoot>
                </table>
            )}
        </div>
    );
}
