/**
 * Per-response runtime parsers. Each parser takes `unknown` and returns a typed
 * value, throwing if the input doesn't match the expected shape. Composed from
 * `validate.ts` primitives.
 */

import type {
    Account,
    AuditEntry,
    AuditUser,
    BudgetTarget,
    Category,
    CategoryGroup,
    CategoryNameHistoryEntry,
    ColumnMapping,
    CommitResult,
    DeactivateResult,
    Disposition,
    DeletionPreview,
    HardDeleteResult,
    IncomeMonth,
    IncomeMonthPaycheck,
    IncomePinResult,
    IncomeSource,
    LoanTerms,
    NetWorthHistory,
    NetWorthClassEntry,
    NetWorthMonth,
    NetWorthPrefill,
    NetWorthRow,
    NetWorthTotals,
    OneTimeLine,
    Paycheck,
    PairWith,
    ParsedTransaction,
    Person,
    PinResult,
    ReviewReason,
    RowValidation,
    RowValidationIssue,
    SuggestedBy,
    Summary,
    SummaryCategory,
    Transaction,
    UploadResult,
} from "./types";
import { BUDGET_ROLES, DEDUCTION_KINDS, DISPOSITIONS } from "./types";
import {
    arrayOf,
    isObject,
    optionalBoolean,
    optionalString,
    requireBoolean,
    requireNumber,
    requireObject,
    requireString,
    stringArray,
} from "./validate";

function parseCategoryNameHistoryEntry(raw: unknown): CategoryNameHistoryEntry {
    const obj = requireObject(raw, "CategoryNameHistoryEntry");
    return {
        previousName: requireString(obj, "previousName", "CategoryNameHistoryEntry"),
        replacedAt: requireString(obj, "replacedAt", "CategoryNameHistoryEntry"),
    };
}

export function parseCategoryGroup(raw: unknown): CategoryGroup {
    const obj = requireObject(raw, "CategoryGroup");
    // An old row can still hold an `active` boolean, from the earlier soft-delete design.
    // We ignore it. A group now has one state only: it exists, or it does not.
    return {
        groupId: requireString(obj, "groupId", "CategoryGroup"),
        name: requireString(obj, "name", "CategoryGroup"),
        order: requireNumber(obj, "order", "CategoryGroup"),
        createdAt: requireString(obj, "createdAt", "CategoryGroup"),
        updatedAt: requireString(obj, "updatedAt", "CategoryGroup"),
        // `system` defaults to false for pre-CP12.5 rows lacking the field.
        system: obj.system === undefined ? false : requireBoolean(obj, "system", "CategoryGroup"),
    };
}

export function parseCategoryGroupArray(raw: unknown): CategoryGroup[] {
    return arrayOf(raw, parseCategoryGroup, "CategoryGroup[]");
}

export function parseCategory(raw: unknown): Category {
    const obj = requireObject(raw, "Category");
    return {
        categoryId: requireString(obj, "categoryId", "Category"),
        name: requireString(obj, "name", "Category"),
        active: requireBoolean(obj, "active", "Category"),
        createdAt: requireString(obj, "createdAt", "Category"),
        updatedAt: requireString(obj, "updatedAt", "Category"),
        // Pre-CP8 rows lack nameHistory; pre-CP12 rows lack groupId + orderInGroup.
        // Both backfill scripts populate these on existing rows; tolerate missing
        // values here so a partially-migrated table doesn't break the UI.
        nameHistory:
            obj.nameHistory === undefined
                ? []
                : arrayOf(obj.nameHistory, parseCategoryNameHistoryEntry, "Category.nameHistory"),
        groupId: typeof obj.groupId === "string" ? obj.groupId : "",
        orderInGroup: typeof obj.orderInGroup === "number" ? obj.orderInGroup : 0,
    };
}

export function parseCategoryArray(raw: unknown): Category[] {
    return arrayOf(raw, parseCategory, "Category[]");
}

export function parseDeactivateResult(raw: unknown): DeactivateResult {
    const obj = requireObject(raw, "DeactivateResult");
    return {
        category: parseCategory(obj.category),
        affectedMonths: stringArray(obj.affectedMonths, "DeactivateResult.affectedMonths"),
    };
}

export function parseDeletionPreview(raw: unknown): DeletionPreview {
    const obj = requireObject(raw, "DeletionPreview");
    const preview = requireObject(obj.deletionPreview, "DeletionPreview.deletionPreview");
    return {
        category: parseCategory(obj.category),
        deletionPreview: {
            budgetRows: requireNumber(preview, "budgetRows", "DeletionPreview.deletionPreview"),
            transactions: requireNumber(preview, "transactions", "DeletionPreview.deletionPreview"),
            lockedMonthsAffected: stringArray(
                preview.lockedMonthsAffected,
                "DeletionPreview.deletionPreview.lockedMonthsAffected",
            ),
        },
    };
}

export function parseHardDeleteResult(raw: unknown): HardDeleteResult {
    const obj = requireObject(raw, "HardDeleteResult");
    return {
        categoryId: requireString(obj, "categoryId", "HardDeleteResult"),
        name: requireString(obj, "name", "HardDeleteResult"),
        budgetRowsDeleted: requireNumber(obj, "budgetRowsDeleted", "HardDeleteResult"),
        transactionsDeleted: requireNumber(obj, "transactionsDeleted", "HardDeleteResult"),
    };
}

export function parseBudgetTarget(raw: unknown): BudgetTarget {
    const obj = requireObject(raw, "BudgetTarget");
    // Densification writes `amount=null` for cells with no walk-back history
    // (architecture: "New category, no prior history"). Null means "no target";
    // the UI surfaces it as $0 because BudgetPage already coerces with `?? 0`.
    const rawAmount = obj.amount;
    let amount: number;
    if (rawAmount === null) {
        amount = 0;
    } else if (typeof rawAmount === "number") {
        amount = rawAmount;
    } else {
        throw new Error(`BudgetTarget.amount must be a number or null, got ${typeof rawAmount}`);
    }
    return {
        yearMonth: requireString(obj, "yearMonth", "BudgetTarget"),
        categoryId: requireString(obj, "categoryId", "BudgetTarget"),
        amount,
        pinned: optionalBoolean(obj, "pinned", "BudgetTarget"),
    };
}

export function parseBudgetTargetArray(raw: unknown): BudgetTarget[] {
    return arrayOf(raw, parseBudgetTarget, "BudgetTarget[]");
}

function parseDownstreamPin(raw: unknown): { categoryId: string; yearMonth: string } {
    const obj = requireObject(raw, "DownstreamPin");
    return {
        categoryId: requireString(obj, "categoryId", "DownstreamPin"),
        yearMonth: requireString(obj, "yearMonth", "DownstreamPin"),
    };
}

export function parsePinResult(raw: unknown): PinResult {
    const obj = requireObject(raw, "PinResult");
    const warnings = requireObject(obj.warnings, "PinResult.warnings");
    return {
        targets: arrayOf(obj.targets, parseBudgetTarget, "PinResult.targets"),
        warnings: {
            downstreamPins: arrayOf(warnings.downstreamPins, parseDownstreamPin, "PinResult.warnings.downstreamPins"),
            pinMatchesCarriedValue: stringArray(
                warnings.pinMatchesCarriedValue,
                "PinResult.warnings.pinMatchesCarriedValue",
            ),
        },
    };
}

function parseDisposition(obj: Record<string, unknown>, parentLabel: string): Disposition {
    // An old row has no disposition. It is a BUDGET row.
    const raw = optionalString(obj, "disposition", parentLabel);
    if (raw === undefined) return "BUDGET";
    const match = DISPOSITIONS.find((d) => d === raw);
    if (match === undefined) {
        throw new Error(`${parentLabel}.disposition: unexpected value '${raw}'`);
    }
    return match;
}

export function parseTransaction(raw: unknown): Transaction {
    const obj = requireObject(raw, "Transaction");
    return {
        yearMonth: requireString(obj, "yearMonth", "Transaction"),
        sortId: requireString(obj, "sortId", "Transaction"),
        transactionDate: requireString(obj, "transactionDate", "Transaction"),
        description: requireString(obj, "description", "Transaction"),
        amount: requireNumber(obj, "amount", "Transaction"),
        disposition: parseDisposition(obj, "Transaction"),
        categoryId: optionalString(obj, "categoryId", "Transaction"),
        accountId: optionalString(obj, "accountId", "Transaction"),
        note: optionalString(obj, "note", "Transaction"),
        createdAt: requireString(obj, "createdAt", "Transaction"),
    };
}

export function parseTransactionArray(raw: unknown): Transaction[] {
    return arrayOf(raw, parseTransaction, "Transaction[]");
}

function parseSummaryCategory(raw: unknown): SummaryCategory {
    const obj = requireObject(raw, "SummaryCategory");
    const historicalRaw = obj.historicalName;
    let historicalName: string | null;
    if (historicalRaw === null) {
        historicalName = null;
    } else if (typeof historicalRaw === "string") {
        historicalName = historicalRaw;
    } else {
        throw new Error(`SummaryCategory.historicalName must be string or null, got ${typeof historicalRaw}`);
    }
    return {
        categoryId: requireString(obj, "categoryId", "SummaryCategory"),
        name: requireString(obj, "name", "SummaryCategory"),
        historicalName,
        budgeted: requireNumber(obj, "budgeted", "SummaryCategory"),
        actual: requireNumber(obj, "actual", "SummaryCategory"),
        delta: requireNumber(obj, "delta", "SummaryCategory"),
    };
}

function parseSummaryTotals(raw: unknown): { budgeted: number; actual: number; delta: number } {
    const obj = requireObject(raw, "Summary.totals");
    return {
        budgeted: requireNumber(obj, "budgeted", "Summary.totals"),
        actual: requireNumber(obj, "actual", "Summary.totals"),
        delta: requireNumber(obj, "delta", "Summary.totals"),
    };
}

function parseSummaryOffBudget(raw: unknown): { actual: number } {
    const obj = requireObject(raw, "Summary.offBudget");
    return { actual: requireNumber(obj, "actual", "Summary.offBudget") };
}

function parseSummaryTotalsWithOffBudget(raw: unknown): { budgeted: number; actual: number } {
    const obj = requireObject(raw, "Summary.totalsWithOffBudget");
    return {
        budgeted: requireNumber(obj, "budgeted", "Summary.totalsWithOffBudget"),
        actual: requireNumber(obj, "actual", "Summary.totalsWithOffBudget"),
    };
}

function parseSummaryIncome(raw: unknown): { plannedNetIncome: number; leftToAssign: number } {
    const obj = requireObject(raw, "Summary.income");
    return {
        plannedNetIncome: requireNumber(obj, "plannedNetIncome", "Summary.income"),
        leftToAssign: requireNumber(obj, "leftToAssign", "Summary.income"),
    };
}

export function parseSummary(raw: unknown): Summary {
    const obj = requireObject(raw, "Summary");
    const state = requireString(obj, "state", "Summary");
    if (state !== "EDITABLE" && state !== "GRACE" && state !== "LOCKED") {
        throw new Error(`Summary.state: unexpected value '${state}'`);
    }
    return {
        yearMonth: requireString(obj, "yearMonth", "Summary"),
        state,
        categories: arrayOf(obj.categories, parseSummaryCategory, "Summary.categories"),
        totals: parseSummaryTotals(obj.totals),
        offBudget: parseSummaryOffBudget(obj.offBudget),
        totalsWithOffBudget: parseSummaryTotalsWithOffBudget(obj.totalsWithOffBudget),
        income: parseSummaryIncome(obj.income),
    };
}

function parseChanges(raw: unknown): Record<string, unknown> {
    // The shape of the `changes` value depends on the action. See "Audit entry changes
    // payload" in the architecture. CREATE, UPDATE, PIN, and UNPIN use {before, after}.
    // CATEGORY_HARD_DELETE uses {budgetRowsDeleted: N, transactionsDeleted: M,
    // name: string}. The component reads `action` to choose. Here we keep the shape.
    return requireObject(raw, "AuditEntry.changes");
}

function parseAuditUser(raw: unknown): AuditUser {
    const obj = requireObject(raw, "AuditEntry.user");
    return {
        sub: requireString(obj, "sub", "AuditEntry.user"),
        email: requireString(obj, "email", "AuditEntry.user"),
        username: requireString(obj, "username", "AuditEntry.user"),
    };
}

function parseNullableYearMonth(raw: unknown): string | null {
    // effectiveYearMonth is null for actions not scoped to a single month
    // (e.g. CATEGORY_HARD_DELETE per the architecture doc).
    if (raw === null) return null;
    if (typeof raw !== "string") {
        throw new Error(`AuditEntry.effectiveYearMonth must be a string or null, got ${typeof raw}`);
    }
    return raw;
}

export function parseAuditEntry(raw: unknown): AuditEntry {
    const obj = requireObject(raw, "AuditEntry");
    return {
        sortId: requireString(obj, "sortId", "AuditEntry"),
        changedAt: requireString(obj, "changedAt", "AuditEntry"),
        effectiveYearMonth: parseNullableYearMonth(obj.effectiveYearMonth),
        categoryId: optionalString(obj, "categoryId", "AuditEntry"),
        incomeSourceId: optionalString(obj, "incomeSourceId", "AuditEntry"),
        incomeLineId: optionalString(obj, "incomeLineId", "AuditEntry"),
        action: requireString(obj, "action", "AuditEntry"),
        explanation: requireString(obj, "explanation", "AuditEntry"),
        user: parseAuditUser(obj.user),
        override: requireBoolean(obj, "override", "AuditEntry"),
        changes: parseChanges(obj.changes),
    };
}

export function parseAuditEntryArray(raw: unknown): AuditEntry[] {
    return arrayOf(raw, parseAuditEntry, "AuditEntry[]");
}

function parseColumnMapping(raw: unknown): ColumnMapping {
    const obj = requireObject(raw, "ColumnMapping");
    return {
        date: requireString(obj, "date", "ColumnMapping"),
        description: requireString(obj, "description", "ColumnMapping"),
        amount: requireString(obj, "amount", "ColumnMapping"),
        amount_invert: optionalBoolean(obj, "amount_invert", "ColumnMapping"),
    };
}

const _SUGGESTED_BY = new Set<string>(["PAIR", "LAST_FOUR", "BANK", "LLM", "DEFAULT"]);

function parsePairWith(raw: unknown): PairWith {
    if (raw === null || raw === undefined) return null;
    const obj = requireObject(raw, "ParsedTransaction.pairWith");
    if (typeof obj.uploadIndex === "number") return { uploadIndex: obj.uploadIndex };
    return {
        yearMonth: requireString(obj, "yearMonth", "ParsedTransaction.pairWith"),
        sortId: requireString(obj, "sortId", "ParsedTransaction.pairWith"),
    };
}

function parseParsedTransaction(raw: unknown): ParsedTransaction {
    const obj = requireObject(raw, "ParsedTransaction");
    const suggestedBy = requireString(obj, "suggestedBy", "ParsedTransaction");
    if (!_SUGGESTED_BY.has(suggestedBy)) {
        throw new Error(`ParsedTransaction.suggestedBy: unexpected value '${suggestedBy}'`);
    }
    return {
        transactionDate: requireString(obj, "transactionDate", "ParsedTransaction"),
        description: requireString(obj, "description", "ParsedTransaction"),
        amount: requireNumber(obj, "amount", "ParsedTransaction"),
        categoryId: optionalString(obj, "categoryId", "ParsedTransaction") ?? "",
        categoryName: optionalString(obj, "categoryName", "ParsedTransaction") ?? "",
        accountId: requireString(obj, "accountId", "ParsedTransaction"),
        disposition: parseDisposition(obj, "ParsedTransaction"),
        suggestedBy: suggestedBy as SuggestedBy,
        pairWith: parsePairWith(obj.pairWith),
        fingerprint: requireString(obj, "fingerprint", "ParsedTransaction"),
        duplicate: requireBoolean(obj, "duplicate", "ParsedTransaction"),
    };
}

const _ROW_VALIDATION_ISSUES = new Set<string>(["missing_category", "locked_month"]);
const _REVIEW_REASONS = new Set<string>(["no_pair_found", "llm_guess", "many_pairs"]);

function parseRowValidation(raw: unknown): RowValidation {
    const obj = requireObject(raw, "RowValidation");
    const issuesRaw = stringArray(obj.issues, "RowValidation.issues");
    // The commit's 409 response has no `review` list. An unknown reason is dropped, because a
    // reason only puts a row first on the review screen.
    const reviewRaw = obj.review === undefined ? [] : stringArray(obj.review, "RowValidation.review");
    return {
        index: requireNumber(obj, "index", "RowValidation"),
        issues: issuesRaw.map((s) => {
            if (!_ROW_VALIDATION_ISSUES.has(s)) {
                throw new Error(`RowValidation.issues: unknown issue '${s}'`);
            }
            return s as RowValidationIssue;
        }),
        review: reviewRaw.filter((s) => _REVIEW_REASONS.has(s)) as ReviewReason[],
    };
}

export function parseUploadResult(raw: unknown): UploadResult {
    const obj = requireObject(raw, "UploadResult");
    return {
        accountId: requireString(obj, "accountId", "UploadResult"),
        columnMapping: parseColumnMapping(obj.columnMapping),
        transactions: arrayOf(obj.transactions, parseParsedTransaction, "UploadResult.transactions"),
        validations: arrayOf(obj.validations, parseRowValidation, "UploadResult.validations"),
    };
}

export function parseCommitResult(raw: unknown): CommitResult {
    const obj = requireObject(raw, "CommitResult");
    return {
        count: requireNumber(obj, "count", "CommitResult"),
        transactions: arrayOf(obj.transactions, parseTransaction, "CommitResult.transactions"),
        skippedDuplicates:
            obj.skippedDuplicates === undefined
                ? []
                : arrayOf(
                      obj.skippedDuplicates,
                      (item) => {
                          if (typeof item !== "number") throw new Error("CommitResult.skippedDuplicates: not a number");
                          return item;
                      },
                      "CommitResult.skippedDuplicates",
                  ),
    };
}

/** Best-effort extraction of `validations` from a 409 response body string.
 * Returns null if the body isn't the expected shape — the caller falls back
 * to showing the raw error message.
 */
export function parseCommitValidationsFromError(responseBody: string): RowValidation[] | null {
    try {
        const raw = JSON.parse(responseBody);
        if (!isObject(raw) || !Array.isArray(raw.validations)) return null;
        return arrayOf(raw.validations, parseRowValidation, "CommitValidationError.validations");
    } catch {
        return null;
    }
}

export function parseDeletedCount(raw: unknown): { deleted: number } {
    const obj = requireObject(raw, "{deleted}");
    return {
        deleted: requireNumber(obj, "deleted", "{deleted}"),
    };
}

export function parseDeletedGroup(raw: unknown): { groupId: string } {
    const obj = requireObject(raw, "{groupId}");
    return {
        groupId: requireString(obj, "groupId", "{groupId}"),
    };
}

// --- The net worth record ---

// --- The income plan ---

export function parseIncomeSource(raw: unknown): IncomeSource {
    const obj = requireObject(raw, "IncomeSource");
    return {
        incomeSourceId: requireString(obj, "incomeSourceId", "IncomeSource"),
        personId: requireString(obj, "personId", "IncomeSource"),
        label: requireString(obj, "label", "IncomeSource"),
        frequency: requireString(obj, "frequency", "IncomeSource"),
        active: requireBoolean(obj, "active", "IncomeSource"),
        sortOrder: requireNumber(obj, "sortOrder", "IncomeSource"),
    };
}

export function parseIncomeSourceArray(raw: unknown): IncomeSource[] {
    return arrayOf(raw, parseIncomeSource, "IncomeSource[]");
}

export function parseDeactivateIncomeSourceResult(raw: unknown): {
    incomeSource: IncomeSource;
    affectedMonths: string[];
} {
    const obj = requireObject(raw, "DeactivateIncomeSourceResult");
    return {
        incomeSource: parseIncomeSource(obj.incomeSource),
        affectedMonths: stringArray(obj.affectedMonths, "DeactivateIncomeSourceResult.affectedMonths"),
    };
}

function parsePaycheckFields(obj: Record<string, unknown>, label: string): Paycheck {
    return {
        paychecksPerMonth: requireNumber(obj, "paychecksPerMonth", label),
        gross: requireNumber(obj, "gross", label),
        deductions: arrayOf(
            obj.deductions,
            (item, index) => {
                const d = requireObject(item, `${label}.deductions[${index}]`);
                const kind = requireString(d, "kind", `${label}.deductions[${index}]`);
                const match = DEDUCTION_KINDS.find((k) => k === kind);
                if (match === undefined) throw new Error(`${label}.deductions[${index}].kind: unexpected '${kind}'`);
                return {
                    label: requireString(d, "label", `${label}.deductions[${index}]`),
                    kind: match,
                    amount: requireNumber(d, "amount", `${label}.deductions[${index}]`),
                };
            },
            `${label}.deductions`,
        ),
    };
}

function parseIncomeMonthPaycheck(raw: unknown): IncomeMonthPaycheck {
    const obj = requireObject(raw, "IncomeMonth.paychecks[]");
    return {
        ...parsePaycheckFields(obj, "IncomeMonth.paychecks[]"),
        incomeSourceId: requireString(obj, "incomeSourceId", "IncomeMonth.paychecks[]"),
        label: requireString(obj, "label", "IncomeMonth.paychecks[]"),
        personId: requireString(obj, "personId", "IncomeMonth.paychecks[]"),
        active: requireBoolean(obj, "active", "IncomeMonth.paychecks[]"),
        net: requireNumber(obj, "net", "IncomeMonth.paychecks[]"),
        monthlyNet: requireNumber(obj, "monthlyNet", "IncomeMonth.paychecks[]"),
        pinned: optionalBoolean(obj, "pinned", "IncomeMonth.paychecks[]"),
    };
}

export function parseOneTimeLine(raw: unknown): OneTimeLine {
    const obj = requireObject(raw, "OneTimeLine");
    return {
        lineId: requireString(obj, "lineId", "OneTimeLine"),
        label: requireString(obj, "label", "OneTimeLine"),
        amount: requireNumber(obj, "amount", "OneTimeLine"),
    };
}

export function parseIncomeMonth(raw: unknown): IncomeMonth {
    const obj = requireObject(raw, "IncomeMonth");
    const state = requireString(obj, "state", "IncomeMonth");
    if (state !== "EDITABLE" && state !== "GRACE" && state !== "LOCKED") {
        throw new Error(`IncomeMonth.state: unexpected value '${state}'`);
    }
    return {
        yearMonth: requireString(obj, "yearMonth", "IncomeMonth"),
        state,
        paychecks: arrayOf(obj.paychecks, parseIncomeMonthPaycheck, "IncomeMonth.paychecks"),
        oneTime: arrayOf(obj.oneTime, parseOneTimeLine, "IncomeMonth.oneTime"),
        plannedNetIncome: requireNumber(obj, "plannedNetIncome", "IncomeMonth"),
    };
}

export function parseIncomePinResult(raw: unknown): IncomePinResult {
    const obj = requireObject(raw, "IncomePinResult");
    const warnings = requireObject(obj.warnings, "IncomePinResult.warnings");
    return {
        income: parseIncomeMonth(obj.income),
        warnings: {
            downstreamPins: arrayOf(
                warnings.downstreamPins,
                (item) => {
                    const pin = requireObject(item, "IncomePinResult.warnings.downstreamPins[]");
                    return {
                        incomeSourceId: requireString(pin, "incomeSourceId", "downstreamPins[]"),
                        yearMonth: requireString(pin, "yearMonth", "downstreamPins[]"),
                    };
                },
                "IncomePinResult.warnings.downstreamPins",
            ),
            pinMatchesCarriedValue: stringArray(
                warnings.pinMatchesCarriedValue,
                "IncomePinResult.warnings.pinMatchesCarriedValue",
            ),
        },
    };
}

export function parseDeletedOneTimeLine(raw: unknown): { deleted: string } {
    const obj = requireObject(raw, "{deleted}");
    return { deleted: requireString(obj, "deleted", "{deleted}") };
}

export function parsePerson(raw: unknown): Person {
    const obj = requireObject(raw, "Person");
    return {
        personId: requireString(obj, "personId", "Person"),
        name: requireString(obj, "name", "Person"),
        birthYearMonth: requireString(obj, "birthYearMonth", "Person"),
        createdAt: requireString(obj, "createdAt", "Person"),
        updatedAt: requireString(obj, "updatedAt", "Person"),
    };
}

export function parsePersonArray(raw: unknown): Person[] {
    return arrayOf(raw, parsePerson, "Person[]");
}

export function parseDeletedPerson(raw: unknown): { personId: string } {
    const obj = requireObject(raw, "{personId}");
    return {
        personId: requireString(obj, "personId", "{personId}"),
    };
}

function parseLoanTerms(raw: unknown): LoanTerms {
    const obj = requireObject(raw, "LoanTerms");
    return {
        interestRate: requireNumber(obj, "interestRate", "LoanTerms"),
        monthlyPayment: requireNumber(obj, "monthlyPayment", "LoanTerms"),
        payoffYearMonth: requireString(obj, "payoffYearMonth", "LoanTerms"),
    };
}

export function parseAccount(raw: unknown): Account {
    const obj = requireObject(raw, "Account");
    const account: Account = {
        accountId: requireString(obj, "accountId", "Account"),
        name: requireString(obj, "name", "Account"),
        accountType: requireString(obj, "accountType", "Account"),
        assetClasses: stringArray(obj.assetClasses, "Account.assetClasses"),
        liability: requireBoolean(obj, "liability", "Account"),
        active: requireBoolean(obj, "active", "Account"),
        sortOrder: requireNumber(obj, "sortOrder", "Account"),
        createdAt: requireString(obj, "createdAt", "Account"),
        updatedAt: requireString(obj, "updatedAt", "Account"),
        // Every account has one or more owners. Tolerate absence (default []) only
        // to survive any hypothetical legacy row; new writes always include it.
        owners: obj.owners === undefined ? [] : stringArray(obj.owners, "Account.owners"),
        // Pre-feature rows lack this flag; default to included (false).
        excludedFromNetWorth: obj.excludedFromNetWorth === true,
    };
    // Optional attributes are omitted server-side when absent (a row only carries
    // what was actually asserted), so tolerate their absence here.
    const notes = optionalString(obj, "notes", "Account");
    if (notes !== undefined) account.notes = notes;
    const budgetRole = optionalString(obj, "budgetRole", "Account");
    const role = BUDGET_ROLES.find((r) => r === budgetRole);
    if (role !== undefined) account.budgetRole = role;
    if (obj.lastFour !== undefined) account.lastFour = stringArray(obj.lastFour, "Account.lastFour");
    if (typeof obj.targetYear === "number") account.targetYear = obj.targetYear;
    if (obj.loanTerms !== undefined) account.loanTerms = parseLoanTerms(obj.loanTerms);
    return account;
}

export function parseAccountArray(raw: unknown): Account[] {
    return arrayOf(raw, parseAccount, "Account[]");
}

function parseNetWorthPrefill(raw: unknown): NetWorthPrefill | null {
    if (raw === null || raw === undefined) return null;
    const obj = requireObject(raw, "NetWorthPrefill");
    return {
        value: requireNumber(obj, "value", "NetWorthPrefill"),
        fromYearMonth: requireString(obj, "fromYearMonth", "NetWorthPrefill"),
    };
}

function parseNullableValue(raw: unknown, label: string): number | null {
    if (raw === null || raw === undefined) return null;
    if (typeof raw === "number") return raw;
    throw new Error(`${label} must be a number or null, got ${typeof raw}`);
}

function parseNetWorthClassEntry(raw: unknown): NetWorthClassEntry {
    const obj = requireObject(raw, "NetWorthClassEntry");
    return {
        assetClass: requireString(obj, "assetClass", "NetWorthClassEntry"),
        value: parseNullableValue(obj.value, "NetWorthClassEntry.value"),
        prefill: parseNetWorthPrefill(obj.prefill),
    };
}

function parseNetWorthRow(raw: unknown): NetWorthRow {
    const obj = requireObject(raw, "NetWorthRow");
    return {
        accountId: requireString(obj, "accountId", "NetWorthRow"),
        note: typeof obj.note === "string" ? obj.note : null,
        classes: arrayOf(obj.classes, parseNetWorthClassEntry, "NetWorthRow.classes"),
    };
}

export function parseNetWorthMonth(raw: unknown): NetWorthMonth {
    const obj = requireObject(raw, "NetWorthMonth");
    return {
        yearMonth: requireString(obj, "yearMonth", "NetWorthMonth"),
        rows: arrayOf(obj.rows, parseNetWorthRow, "NetWorthMonth.rows"),
    };
}

function parseNetWorthTotals(raw: unknown, label: string): NetWorthTotals {
    const obj = requireObject(raw, label);
    return {
        assets: requireNumber(obj, "assets", label),
        liabilities: requireNumber(obj, "liabilities", label),
        netWorth: requireNumber(obj, "netWorth", label),
    };
}

export function parseNetWorthHistory(raw: unknown): NetWorthHistory {
    const obj = requireObject(raw, "NetWorthHistory");
    const accounts = arrayOf(obj.accounts, parseAccount, "NetWorthHistory.accounts");
    const months = stringArray(obj.months, "NetWorthHistory.months");

    const valuesObj = requireObject(obj.values, "NetWorthHistory.values");
    const values: Record<string, Record<string, number>> = {};
    for (const ym of Object.keys(valuesObj)) {
        const inner = requireObject(valuesObj[ym], `NetWorthHistory.values.${ym}`);
        const row: Record<string, number> = {};
        for (const accountId of Object.keys(inner)) {
            const v = inner[accountId];
            if (typeof v !== "number") {
                throw new Error(`NetWorthHistory.values.${ym}.${accountId} must be a number`);
            }
            row[accountId] = v;
        }
        values[ym] = row;
    }

    // `notes` may be absent on responses from an older backend — default to {}.
    const notesObj = obj.notes === undefined ? {} : requireObject(obj.notes, "NetWorthHistory.notes");
    const notes: Record<string, Record<string, string>> = {};
    for (const ym of Object.keys(notesObj)) {
        const inner = requireObject(notesObj[ym], `NetWorthHistory.notes.${ym}`);
        const row: Record<string, string> = {};
        for (const accountId of Object.keys(inner)) {
            const v = inner[accountId];
            if (typeof v !== "string") {
                throw new Error(`NetWorthHistory.notes.${ym}.${accountId} must be a string`);
            }
            row[accountId] = v;
        }
        notes[ym] = row;
    }

    const totalsObj = requireObject(obj.totals, "NetWorthHistory.totals");
    const totals: Record<string, NetWorthTotals> = {};
    for (const ym of Object.keys(totalsObj)) {
        totals[ym] = parseNetWorthTotals(totalsObj[ym], `NetWorthHistory.totals.${ym}`);
    }

    return { accounts, months, values, notes, totals };
}
