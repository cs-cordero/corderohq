import { getAccessToken } from "./auth";
import { getSimulatedDate } from "./simulated_date";
import {
    parseAccount,
    parseAccountArray,
    parseAuditEntryArray,
    parseBudgetTargetArray,
    parseBudgetUpdateResult,
    parseCategory,
    parseCategoryArray,
    parseCategoryGroup,
    parseCategoryGroupArray,
    parseCommitResult,
    parseDeactivateResult,
    parseDeletedCount,
    parseDeletedGroup,
    parseDeletedPerson,
    parseDeletionPreview,
    parseDeactivateIncomeSourceResult,
    parseDeletedOneTimeIncome,
    parseHardDeleteResult,
    parseIncomeMonth,
    parseIncomeSource,
    parseIncomeSourceArray,
    parseIncomeUpdateResult,
    parseNetWorthHistory,
    parseNetWorthMonth,
    parseOneTimeIncomeItem,
    parsePerson,
    parsePersonArray,
    parseSummary,
    parseTransaction,
    parseTransactionArray,
    parseUploadResult,
} from "./parsers";
import type {
    AccountCreate,
    AccountUpdate,
    BudgetTreatment,
    OneTimeIncome,
    RecurringIncomeChange,
    Transaction,
} from "./types";

const API_BASE = "/api";

export class ApiError extends Error {
    constructor(
        message: string,
        public readonly status: number,
        public readonly url: string,
        public readonly responseBody: string,
    ) {
        super(message);
        this.name = "ApiError";
    }
}

async function request<T>(path: string, parse: (raw: unknown) => T, options: RequestInit = {}): Promise<T> {
    const token = await getAccessToken();
    const url = `${API_BASE}${path}`;
    // X-Simulated-Date works in dev only. See simulated_date.ts. Outside dev mode,
    // getSimulatedDate returns null, so a prod request never carries the header.
    const simulatedDate = getSimulatedDate();
    const response = await fetch(url, {
        ...options,
        headers: {
            Authorization: `Bearer ${token}`,
            ...(simulatedDate ? { "X-Simulated-Date": simulatedDate } : {}),
            ...options.headers,
        },
    });

    if (!response.ok) {
        const text = await response.text();
        throw new ApiError(text || `Request failed: ${response.status}`, response.status, url, text);
    }

    const text = await response.text();
    const raw: unknown = text ? JSON.parse(text) : undefined;
    return parse(raw);
}

function jsonBody(data: unknown): RequestInit {
    return {
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
    };
}

// Category groups
export const fetchCategoryGroups = () => request("/category-groups", parseCategoryGroupArray);
export const createCategoryGroup = (name: string) =>
    request("/category-groups", parseCategoryGroup, { method: "POST", ...jsonBody({ name }) });
export const renameCategoryGroup = (id: string, name: string) =>
    request(`/category-groups/${id}`, parseCategoryGroup, { method: "PUT", ...jsonBody({ name }) });
export const deleteCategoryGroup = (id: string) =>
    request(`/category-groups/${id}`, parseDeletedGroup, { method: "DELETE" });
export const reorderCategoryGroups = (order: string[]) =>
    request("/category-groups/reorder", parseCategoryGroupArray, {
        method: "POST",
        ...jsonBody({ order }),
    });

// Categories
export const fetchCategories = () => request("/categories", parseCategoryArray);
export const fetchAllCategories = () => request("/categories?include_inactive=true", parseCategoryArray);
export const createCategory = (name: string, groupId: string, initialTarget = 0) =>
    request("/categories", parseCategory, {
        method: "POST",
        ...jsonBody({ name, initialTarget, groupId }),
    });
export const updateCategory = (id: string, updates: { name?: string; groupId?: string }) =>
    request(`/categories/${id}`, parseCategory, { method: "PUT", ...jsonBody(updates) });
export const reorderCategoriesInGroup = (groupId: string, order: string[]) =>
    request("/categories/reorder", parseCategoryArray, {
        method: "POST",
        ...jsonBody({ groupId, order }),
    });
export const deactivateCategory = (id: string, opts: { confirm?: boolean; explanation?: string } = {}) =>
    request(`/categories/${id}/deactivate`, parseDeactivateResult, {
        method: "POST",
        ...jsonBody(opts),
    });
export const reactivateCategory = (id: string) =>
    request(`/categories/${id}/reactivate`, parseCategory, { method: "POST" });
export const fetchCategoryDeletionPreview = (id: string) =>
    request(`/categories/${id}?deletion_preview=true`, parseDeletionPreview);
export const hardDeleteCategory = (id: string, opts: { confirm: true; confirmName: string; explanation: string }) =>
    request(`/categories/${id}`, parseHardDeleteResult, {
        method: "DELETE",
        ...jsonBody(opts),
    });

// Budget
export const fetchBudget = (yearMonth: string) => request(`/budget/${yearMonth}`, parseBudgetTargetArray);
export const replaceBudget = (
    yearMonth: string,
    // `pinnedAmount`: a number sets the pinned amount, null clears it, and absent keeps it.
    targets: { categoryId: string; carriedAmount: number; pinnedAmount?: number | null }[],
    explanation: string,
    override = false,
) =>
    request(`/budget/${yearMonth}/replace`, parseBudgetTargetArray, {
        method: "POST",
        ...jsonBody({ targets, explanation, ...(override ? { override: true } : {}) }),
    });
export const updateBudget = (
    yearMonth: string,
    // Each key is optional. A number sets that value, null removes it, and absent keeps it.
    targets: { categoryId: string; carriedAmount?: number | null; pinnedAmount?: number | null }[],
    explanation: string,
) =>
    request(`/budget/${yearMonth}/update`, parseBudgetUpdateResult, {
        method: "POST",
        ...jsonBody({ targets, explanation }),
    });

// Transactions
export const fetchTransactions = (yearMonth: string) =>
    request(`/transactions?month=${yearMonth}`, parseTransactionArray);
export const createTransaction = (
    data: {
        yearMonth: string;
        transactionDate: string;
        description: string;
        amount: number;
        budgetTreatment: BudgetTreatment;
        // Required for BUDGET_IMPACTING, optional for OFF_BUDGET. The server drops it for SKIP.
        categoryId?: string;
        note?: string;
    },
    override = false,
) =>
    request("/transactions", parseTransaction, {
        method: "POST",
        ...jsonBody({ ...data, ...(override ? { override: true } : {}) }),
    });
export const updateTransaction = (
    yearMonth: string,
    sortId: string,
    // An empty string for `categoryId` or `note` removes the value. A change to SKIP removes the
    // category on the server.
    updates: Partial<Pick<Transaction, "description" | "amount" | "budgetTreatment" | "categoryId" | "note">>,
    override = false,
) =>
    request("/transactions/update", parseTransaction, {
        method: "POST",
        ...jsonBody({ yearMonth, sortId, ...updates, ...(override ? { override: true } : {}) }),
    });
export const deleteTransactions = (items: { yearMonth: string; sortId: string }[], override = false) =>
    request("/transactions/delete", parseDeletedCount, {
        method: "POST",
        ...jsonBody({ items, ...(override ? { override: true } : {}) }),
    });
export const uploadTransactions = (csvData: string, accountId: string, flipSigns = false) =>
    request("/transactions/upload", parseUploadResult, {
        method: "POST",
        ...jsonBody({ csvData, accountId, ...(flipSigns ? { flipSigns: true } : {}) }),
    });
export const commitTransactions = (
    rows: {
        transactionDate: string;
        description: string;
        amount: number;
        budgetTreatment: BudgetTreatment;
        categoryId?: string;
        accountId: string;
        note?: string;
        fingerprint: string;
    }[],
    override = false,
) =>
    request("/transactions/commit", parseCommitResult, {
        method: "POST",
        ...jsonBody({ rows, ...(override ? { override: true } : {}) }),
    });

// Income sources and the income plan
export const fetchAllIncomeSources = () => request("/income-sources?include_inactive=true", parseIncomeSourceArray);
export const createIncomeSource = (data: { owners: string[]; label: string }) =>
    request("/income-sources", parseIncomeSource, { method: "POST", ...jsonBody(data) });
export const updateIncomeSource = (id: string, updates: { label?: string; owners?: string[] }) =>
    request(`/income-sources/${id}`, parseIncomeSource, { method: "PUT", ...jsonBody(updates) });
export const reorderIncomeSources = (order: string[]) =>
    request("/income-sources/reorder", parseIncomeSourceArray, { method: "POST", ...jsonBody({ order }) });
export const deactivateIncomeSource = (id: string, opts: { confirm?: boolean; explanation?: string } = {}) =>
    request(`/income-sources/${id}/deactivate`, parseDeactivateIncomeSourceResult, {
        method: "POST",
        ...jsonBody(opts),
    });
export const reactivateIncomeSource = (id: string) =>
    request(`/income-sources/${id}/reactivate`, parseIncomeSource, { method: "POST" });
export const fetchIncomeMonth = (yearMonth: string) => request(`/income/${yearMonth}`, parseIncomeMonth);
export const replaceIncome = (
    yearMonth: string,
    // The body must hold each recurring income with a carried value in the month.
    recurring: RecurringIncomeChange[],
    explanation: string,
    override = false,
) =>
    request(`/income/${yearMonth}/replace`, parseIncomeMonth, {
        method: "POST",
        ...jsonBody({ recurring, explanation, ...(override ? { override: true } : {}) }),
    });
export const updateIncome = (
    yearMonth: string,
    recurring: RecurringIncomeChange[],
    explanation: string,
    // The override only lifts the rules of the end month and of an inactive income source.
    override = false,
) =>
    request(`/income/${yearMonth}/update`, parseIncomeUpdateResult, {
        method: "POST",
        ...jsonBody({ recurring, explanation, ...(override ? { override: true } : {}) }),
    });
export const createOneTimeIncome = (yearMonth: string, income: OneTimeIncome, explanation: string, override = false) =>
    request(`/income/${yearMonth}/one-time`, parseOneTimeIncomeItem, {
        method: "POST",
        ...jsonBody({ ...income, explanation, ...(override ? { override: true } : {}) }),
    });
export const updateOneTimeIncome = (
    yearMonth: string,
    plannedIncomeId: string,
    income: OneTimeIncome,
    explanation: string,
    override = false,
) =>
    request(`/income/${yearMonth}/one-time`, parseOneTimeIncomeItem, {
        method: "POST",
        ...jsonBody({ ...income, plannedIncomeId, explanation, ...(override ? { override: true } : {}) }),
    });
export const deleteOneTimeIncome = (
    yearMonth: string,
    plannedIncomeId: string,
    explanation: string,
    override = false,
) =>
    request(`/income/${yearMonth}/one-time`, parseDeletedOneTimeIncome, {
        method: "POST",
        ...jsonBody({ plannedIncomeId, delete: true, explanation, ...(override ? { override: true } : {}) }),
    });

// Household profile (people)
export const fetchProfile = () => request("/profiles", parsePersonArray);
export const createPerson = (name: string, birthYearMonth: string) =>
    request("/profiles", parsePerson, { method: "POST", ...jsonBody({ name, birthYearMonth }) });
export const updatePerson = (id: string, updates: { name?: string; birthYearMonth?: string }) =>
    request(`/profiles/${id}`, parsePerson, { method: "PUT", ...jsonBody(updates) });
export const deletePerson = (id: string) => request(`/profiles/${id}/delete`, parseDeletedPerson, { method: "POST" });

// Accounts
export const fetchAccounts = () => request("/accounts", parseAccountArray);
export const fetchAllAccounts = () => request("/accounts?include_inactive=true", parseAccountArray);
export const createAccount = (data: AccountCreate) =>
    request("/accounts", parseAccount, { method: "POST", ...jsonBody(data) });
export const updateAccount = (id: string, updates: AccountUpdate) =>
    request(`/accounts/${id}`, parseAccount, { method: "PUT", ...jsonBody(updates) });
export const deactivateAccount = (id: string) =>
    request(`/accounts/${id}/deactivate`, parseAccount, { method: "POST" });
export const reactivateAccount = (id: string) =>
    request(`/accounts/${id}/reactivate`, parseAccount, { method: "POST" });
export const reorderAccounts = (order: string[]) =>
    request("/accounts/reorder", parseAccountArray, { method: "POST", ...jsonBody({ order }) });

// Net worth snapshots
export const fetchNetWorthMonth = (yearMonth: string) => request(`/net-worth/${yearMonth}`, parseNetWorthMonth);
export const fetchNetWorthHistory = () => request("/net-worth/history", parseNetWorthHistory);
export const saveNetWorthMonth = (
    yearMonth: string,
    rows: {
        accountId: string;
        note?: string | null;
        classes: { assetClass: string; value: number | null }[];
    }[],
) => request(`/net-worth/${yearMonth}`, parseNetWorthMonth, { method: "POST", ...jsonBody({ rows }) });

// Summary
export const fetchSummary = (yearMonth: string) => request(`/summary?month=${yearMonth}`, parseSummary);

// Audit log
export const fetchAuditLog = (limit = 10) => request(`/audit-log?limit=${limit}`, parseAuditEntryArray);
