import { getAccessToken } from "./auth";
import { getSimulatedDate } from "./simulated_date";
import {
    parseAccount,
    parseAccountArray,
    parseAuditEntryArray,
    parseBudgetTargetArray,
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
    parseDeletedOneTimeLine,
    parseHardDeleteResult,
    parseIncomeMonth,
    parseIncomePinResult,
    parseIncomeSource,
    parseIncomeSourceArray,
    parseNetWorthHistory,
    parseNetWorthMonth,
    parseOneTimeLine,
    parsePerson,
    parsePersonArray,
    parsePinResult,
    parseSummary,
    parseTransaction,
    parseTransactionArray,
    parseUploadResult,
} from "./parsers";
import type { AccountCreate, AccountUpdate, Disposition, Paycheck, Transaction } from "./types";

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
    targets: { categoryId: string; amount: number }[],
    explanation: string,
    override = false,
) =>
    request(`/budget/${yearMonth}/replace`, parseBudgetTargetArray, {
        method: "POST",
        ...jsonBody({ targets, explanation, ...(override ? { override: true } : {}) }),
    });
export const pinBudget = (
    yearMonth: string,
    targets: { categoryId: string; amount: number | null }[],
    explanation: string,
) =>
    request(`/budget/${yearMonth}/pin`, parsePinResult, {
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
        disposition: Disposition;
        // Required for BUDGET, optional for OFF_BUDGET. The server drops it for SKIP.
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
    updates: Partial<Pick<Transaction, "description" | "amount" | "disposition" | "categoryId" | "note">>,
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
        disposition: Disposition;
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
export const createIncomeSource = (data: {
    personId: string;
    label: string;
    frequency: string;
    initialPaycheck: Paycheck;
    explanation: string;
}) => request("/income-sources", parseIncomeSource, { method: "POST", ...jsonBody(data) });
export const updateIncomeSource = (id: string, updates: { label?: string; frequency?: string }) =>
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
    paychecks: { incomeSourceId: string; paycheck: Paycheck }[],
    explanation: string,
    override = false,
) =>
    request(`/income/${yearMonth}/replace`, parseIncomeMonth, {
        method: "POST",
        ...jsonBody({ paychecks, explanation, ...(override ? { override: true } : {}) }),
    });
export const pinIncome = (
    yearMonth: string,
    paychecks: { incomeSourceId: string; paycheck: Paycheck | null }[],
    explanation: string,
) =>
    request(`/income/${yearMonth}/pin`, parseIncomePinResult, {
        method: "POST",
        ...jsonBody({ paychecks, explanation }),
    });
export const createOneTimeLine = (
    yearMonth: string,
    data: { label: string; amount: number; explanation: string },
    override = false,
) =>
    request(`/income/${yearMonth}/one-time`, parseOneTimeLine, {
        method: "POST",
        ...jsonBody({ ...data, ...(override ? { override: true } : {}) }),
    });
export const updateOneTimeLine = (
    yearMonth: string,
    data: { lineId: string; label: string; amount: number; explanation: string },
    override = false,
) =>
    request(`/income/${yearMonth}/one-time/update`, parseOneTimeLine, {
        method: "POST",
        ...jsonBody({ ...data, ...(override ? { override: true } : {}) }),
    });
export const deleteOneTimeLine = (
    yearMonth: string,
    data: { lineId: string; explanation: string },
    override = false,
) =>
    request(`/income/${yearMonth}/one-time/delete`, parseDeletedOneTimeLine, {
        method: "POST",
        ...jsonBody({ ...data, ...(override ? { override: true } : {}) }),
    });

// Household profile (people)
export const fetchProfile = () => request("/profile", parsePersonArray);
export const createPerson = (name: string, birthYearMonth: string) =>
    request("/profile/people", parsePerson, { method: "POST", ...jsonBody({ name, birthYearMonth }) });
export const updatePerson = (id: string, updates: { name?: string; birthYearMonth?: string }) =>
    request(`/profile/people/${id}`, parsePerson, { method: "PUT", ...jsonBody(updates) });
export const deletePerson = (id: string) =>
    request(`/profile/people/${id}/delete`, parseDeletedPerson, { method: "POST" });

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
