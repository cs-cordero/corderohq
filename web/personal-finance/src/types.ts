export interface CategoryNameHistoryEntry {
    previousName: string;
    replacedAt: string;
}

export interface CategoryGroup {
    groupId: string;
    name: string;
    order: number;
    createdAt: string;
    updatedAt: string;
    // True for sentinel groups managed by the backend (e.g. "Unassigned"). System
    // groups are immutable from the UI: no rename, no delete, no drag-to-reorder.
    system: boolean;
}

export interface Category {
    categoryId: string;
    name: string;
    active: boolean;
    createdAt: string;
    updatedAt: string;
    nameHistory: CategoryNameHistoryEntry[];
    // A row from before CP12 may not hold these two fields. The backfill script writes
    // them, and each new row has them. When a value is absent, the parser uses an empty
    // string or 0. The UI then does not fail on an old row.
    groupId: string;
    orderInGroup: number;
}

export interface DeactivateResult {
    category: Category;
    affectedMonths: string[];
}

export interface DeletionPreview {
    category: Category;
    deletionPreview: {
        budgetRows: number;
        transactions: number;
        lockedMonthsAffected: string[];
    };
}

export interface HardDeleteResult {
    categoryId: string;
    name: string;
    budgetRowsDeleted: number;
    transactionsDeleted: number;
}

export interface BudgetTarget {
    yearMonth: string;
    categoryId: string;
    amount: number;
    // Only present for future-month responses (server-resolved walk-back).
    // True means an explicit pin row exists at this (yearMonth, categoryId);
    // false means the amount was carried forward from an earlier month.
    pinned?: boolean;
}

export interface PinResult {
    targets: BudgetTarget[];
    warnings: {
        downstreamPins: { categoryId: string; yearMonth: string }[];
        pinMatchesCarriedValue: string[];
    };
}

// How the budget counts a transaction. The backend treats an absent value as BUDGET, and the
// parser does the same.
export const DISPOSITIONS = ["BUDGET", "OFF_BUDGET", "SKIP"] as const;
export type Disposition = (typeof DISPOSITIONS)[number];

export const DISPOSITION_LABELS: Record<Disposition, string> = {
    BUDGET: "Budget",
    OFF_BUDGET: "Off-budget",
    SKIP: "Skip",
};

export interface Transaction {
    yearMonth: string;
    sortId: string;
    transactionDate: string;
    description: string;
    amount: number;
    disposition: Disposition;
    // Always present on a BUDGET row. Optional on an OFF_BUDGET row. Absent on a SKIP row.
    categoryId?: string;
    accountId?: string;
    note?: string;
    createdAt: string;
}

export interface SummaryCategory {
    categoryId: string;
    // Current display name. `historicalName` is the name in effect during the
    // requested year-month if the category was later renamed (set only for
    // locked months per architecture's "Display names on locked months").
    name: string;
    historicalName: string | null;
    budgeted: number;
    actual: number;
    delta: number;
}

export interface Summary {
    yearMonth: string;
    state: "EDITABLE" | "GRACE" | "LOCKED";
    categories: SummaryCategory[];
    totals: { budgeted: number; actual: number; delta: number };
    // Not part of `totals`. The budgeted amount of the Off-budget row is always 0.
    offBudget: { actual: number };
    totalsWithOffBudget: { budgeted: number; actual: number };
    // leftToAssign = plannedNetIncome − totals.budgeted. The goal is 0.
    income: { plannedNetIncome: number; leftToAssign: number };
}

export interface AuditUser {
    sub: string;
    email: string;
    username: string;
}

export interface AuditEntry {
    sortId: string;
    changedAt: string;
    effectiveYearMonth: string | null;
    // A budget entry has a categoryId. An income plan entry has incomeLineId, and a paycheck
    // entry also has incomeSourceId.
    categoryId?: string;
    incomeSourceId?: string;
    incomeLineId?: string;
    action: string;
    explanation: string;
    user: AuditUser;
    override: boolean;
    // Action-shaped payload. CREATE/UPDATE/PIN/UNPIN use {before, after} per field.
    // CATEGORY_HARD_DELETE uses {budgetRowsDeleted, transactionsDeleted, name}.
    // Renderer branches on `action`.
    changes: Record<string, unknown>;
}

export interface ColumnMapping {
    date: string;
    description: string;
    amount: string;
    amount_invert?: boolean;
}

// Where a suggestion on the review screen came from.
export type SuggestedBy = "PAIR" | "LAST_FOUR" | "BANK" | "LLM" | "DEFAULT";

// The other side of a transfer: a saved transaction, or another row of the same upload.
export type PairWith = { yearMonth: string; sortId: string } | { uploadIndex: number } | null;

export interface ParsedTransaction {
    transactionDate: string;
    description: string;
    amount: number;
    categoryId: string; // "" when there is no category
    categoryName: string;
    accountId: string;
    disposition: Disposition;
    suggestedBy: SuggestedBy;
    pairWith: PairWith;
    fingerprint: string;
    duplicate: boolean; // the table holds this row already
    note?: string; // set on the review screen only
}

// A closed set, so that the UI can show something different for each issue. If you add an
// issue here, you must also change `_validate_commit_rows` in the backend.
export type RowValidationIssue = "missing_category" | "locked_month";

// The reasons to look at a row. Unlike an issue, a reason does not block the commit.
export type ReviewReason = "no_pair_found" | "llm_guess" | "many_pairs" | "unknown_direction";

export const REVIEW_REASON_LABELS: Record<ReviewReason, string> = {
    no_pair_found: "Transfer? No other side found",
    llm_guess: "Guess — please check",
    many_pairs: "More than one possible other side",
    unknown_direction: "Could not tell where the money went",
};

export interface RowValidation {
    index: number;
    issues: RowValidationIssue[];
    review: ReviewReason[];
}

// How the upload set the sign of the amounts. The app stores money out as positive.
export interface SignCheck {
    llmInverted: boolean; // the LLM's guess from the header names
    locallyFlipped: boolean; // a spending account where most rows came in negative
    userFlipped: boolean; // the "flip all signs" button
}

export interface UploadResult {
    accountId: string;
    signCheck: SignCheck;
    columnMapping: ColumnMapping;
    transactions: ParsedTransaction[];
    validations: RowValidation[];
}

export interface CommitResult {
    count: number;
    transactions: Transaction[];
    skippedDuplicates: number[]; // indices into the committed batch
}

export interface CommitValidationError {
    error: string;
    validations: RowValidation[];
}

// --- The net worth record ---

// A person in the household. Owner references on accounts point at personId.
// --- The income plan ---

export const PAY_FREQUENCIES = ["weekly", "biweekly", "semimonthly", "monthly"] as const;
export type PayFrequency = (typeof PAY_FREQUENCIES)[number];

export const PAY_FREQUENCY_LABELS: Record<PayFrequency, string> = {
    weekly: "Weekly",
    biweekly: "Every two weeks",
    semimonthly: "Twice a month",
    monthly: "Monthly",
};

export const DEDUCTION_KINDS = ["TAX", "PRETAX_SAVINGS", "PRETAX_BENEFIT", "POSTTAX_SAVINGS"] as const;
export type DeductionKind = (typeof DEDUCTION_KINDS)[number];

export const DEDUCTION_KIND_LABELS: Record<DeductionKind, string> = {
    TAX: "Tax",
    PRETAX_SAVINGS: "Pre-tax savings",
    PRETAX_BENEFIT: "Pre-tax benefit",
    POSTTAX_SAVINGS: "Post-tax savings",
};

export interface Deduction {
    label: string;
    kind: DeductionKind;
    amount: number; // millionths, for one paycheck
}

// One paycheck of one income source. The server calculates the net pay; it never stores it.
export interface Paycheck {
    paychecksPerMonth: number;
    gross: number; // millionths, for one paycheck
    deductions: Deduction[];
}

export interface IncomeSource {
    incomeSourceId: string;
    personId: string;
    label: string;
    frequency: string; // one of PAY_FREQUENCIES; kept as string so an unknown DB value doesn't crash the parser
    active: boolean;
    sortOrder: number;
}

export interface IncomeMonthPaycheck extends Paycheck {
    incomeSourceId: string;
    label: string;
    personId: string;
    active: boolean;
    net: number; // one paycheck
    monthlyNet: number; // net × paychecksPerMonth
    pinned?: boolean; // future months only
}

export interface OneTimeLine {
    lineId: string;
    label: string;
    amount: number;
}

export interface IncomeMonth {
    yearMonth: string;
    state: "EDITABLE" | "GRACE" | "LOCKED";
    paychecks: IncomeMonthPaycheck[];
    oneTime: OneTimeLine[];
    plannedNetIncome: number;
}

export interface IncomePinResult {
    income: IncomeMonth;
    warnings: {
        downstreamPins: { incomeSourceId: string; yearMonth: string }[];
        pinMatchesCarriedValue: string[];
    };
}

export interface Person {
    personId: string;
    name: string;
    birthYearMonth: string; // YYYY-MM
    createdAt: string;
    updatedAt: string;
}

// The amortization values for a debt, such as a mortgage, a car loan, or a student loan.
// We hold them for the simulation feature that comes later. Today the UI reads them back
// for one purpose only: so that a user can change them.
export interface LoanTerms {
    interestRate: number; // annual rate as a decimal, e.g. 0.04875 for 4.875%
    monthlyPayment: number; // integer millionths of a dollar (principal + interest)
    payoffYearMonth: string; // YYYY-MM of the final payment
}

export interface Account {
    accountId: string;
    name: string;
    accountType: string; // one of ACCOUNT_TYPES; kept as string so unknown DB values don't crash the parser
    assetClasses: string[]; // one or more ASSET_CLASSES the account holds (fixed types force one)
    liability: boolean;
    active: boolean;
    sortOrder: number;
    owners: string[]; // one or more personIds; a jointly-held account simply has 2+ owners
    // Tracked but left out of the Total Assets / Liabilities / Net Worth sums
    // (e.g. a 529 or custodial account you follow but don't own).
    excludedFromNetWorth: boolean;
    // This is here only when `target_date` is in assetClasses. It is the year of the fund.
    targetYear?: number;
    loanTerms?: LoanTerms;
    notes?: string;
    // The budget role for the CSV import. Absent means the default for the type (see
    // DEFAULT_BUDGET_ROLES).
    budgetRole?: BudgetRole;
    // The "last four digits" of the account, which the CSV import looks for in a description.
    lastFour?: string[];
    createdAt: string;
    updatedAt: string;
}

export const BUDGET_ROLES = ["SPENDING", "HOLDING"] as const;
export type BudgetRole = (typeof BUDGET_ROLES)[number];

export const BUDGET_ROLE_LABELS: Record<BudgetRole, string> = {
    SPENDING: "Spending (checking, credit card)",
    HOLDING: "Holding (savings fund, brokerage, 529)",
};

// The same defaults as _DEFAULT_BUDGET_ROLE in lambda/src/corderohq/networth/models.py.
export const DEFAULT_BUDGET_ROLES: Record<string, BudgetRole> = {
    checking: "SPENDING",
    credit_card: "SPENDING",
    savings: "HOLDING",
    brokerage: "HOLDING",
    "529": "HOLDING",
    "530a": "HOLDING",
};

export function budgetRoleOf(account: Account): BudgetRole | null {
    return account.budgetRole ?? DEFAULT_BUDGET_ROLES[account.accountType] ?? null;
}

// Create payload: required identity/classification fields plus optional extras.
// Optional fields are simply omitted when unset (never sent as null on create).
export interface AccountCreate {
    name: string;
    accountType: string;
    assetClasses: string[]; // for fixed-class types the server overrides this; send the fixed set anyway
    owners: string[]; // required, non-empty
    excludedFromNetWorth?: boolean; // defaults to false server-side
    targetYear?: number; // required iff assetClasses includes target_date
    loanTerms?: LoanTerms;
    notes?: string;
}

// The body for an edit. Every field is optional. For a field that accepts null, a `null`
// value REMOVES the attribute on the server. If the key is absent, the server leaves the
// attribute as it is. `owners` and `assetClasses`, when present, must not be empty, and
// they take the place of the current values. No one can change `accountType` or
// `liability` here.
export type AccountUpdate = Partial<{
    name: string;
    assetClasses: string[];
    owners: string[];
    excludedFromNetWorth: boolean;
    targetYear: number | null;
    loanTerms: LoanTerms | null;
    notes: string | null;
    sortOrder: number;
    budgetRole: BudgetRole | null; // null removes it; the account then uses the default for its type
    lastFour: string[]; // an empty list removes it
}>;

// Enum value spaces mirror lambda/src/corderohq/networth/models.py. The backend
// validates against its StrEnums and 400s on unknown values, so these must stay
// in sync with that file.
export const ACCOUNT_TYPES = [
    "checking",
    "savings",
    "brokerage",
    "401k",
    "403b",
    "roth_ira",
    "traditional_ira",
    "hsa",
    "529",
    "530a",
    "real_estate",
    "mortgage",
    "vehicle",
    "other_asset",
    "other_liability",
    "credit_card",
] as const;

// The asset classes that the account picker offers. A choice of `target_date` also needs
// a target year. See TARGET_YEAR_VINTAGES.
export const ASSET_CLASSES = [
    "cash",
    "us_equity_large_cap",
    "us_equity_small_cap",
    "intl_equity",
    "bonds",
    "fixed_income",
    "real_estate",
    "target_date",
    "other",
] as const;

// Target-date funds are sold in 5-year vintages; the account form offers these.
export const TARGET_YEAR_VINTAGES = [2025, 2030, 2035, 2040, 2045, 2050, 2055, 2060, 2065, 2070] as const;

// Type-driven behavior, mirroring lambda/src/corderohq/networth/models.py's
// _ACCOUNT_TYPE_META. The account type is the "driver": it fixes liability, may
// fix the asset class (and to what), and gates loan terms. The backend is
// authoritative — this mirror only shapes the form. Keep the two in sync.
export interface AccountTypeMeta {
    liability: boolean;
    fixedAssetClass: string | null; // when set, asset class is forced & shown read-only (hidden for liabilities)
    amortizing: boolean; // whether loan-term fields are offered
}

export const ACCOUNT_TYPE_META: Record<string, AccountTypeMeta> = {
    checking: { liability: false, fixedAssetClass: "cash", amortizing: false },
    savings: { liability: false, fixedAssetClass: "cash", amortizing: false },
    brokerage: { liability: false, fixedAssetClass: null, amortizing: false },
    "401k": { liability: false, fixedAssetClass: null, amortizing: false },
    "403b": { liability: false, fixedAssetClass: null, amortizing: false },
    roth_ira: { liability: false, fixedAssetClass: null, amortizing: false },
    traditional_ira: { liability: false, fixedAssetClass: null, amortizing: false },
    hsa: { liability: false, fixedAssetClass: null, amortizing: false },
    "529": { liability: false, fixedAssetClass: null, amortizing: false },
    "530a": { liability: false, fixedAssetClass: null, amortizing: false },
    real_estate: { liability: false, fixedAssetClass: "real_estate", amortizing: false },
    vehicle: { liability: false, fixedAssetClass: "other", amortizing: false },
    mortgage: { liability: true, fixedAssetClass: "debt", amortizing: true },
    other_asset: { liability: false, fixedAssetClass: null, amortizing: false },
    other_liability: { liability: true, fixedAssetClass: "debt", amortizing: true },
    credit_card: { liability: true, fixedAssetClass: "debt", amortizing: false },
};

export const ACCOUNT_TYPE_LABELS: Record<string, string> = {
    checking: "Checking",
    savings: "Savings",
    brokerage: "Brokerage",
    "401k": "401(k)",
    "403b": "403(b)",
    roth_ira: "Roth IRA",
    traditional_ira: "Traditional IRA",
    hsa: "HSA",
    "529": "529",
    "530a": "530A",
    real_estate: "Real Estate",
    mortgage: "Mortgage",
    vehicle: "Vehicle",
    other_asset: "Other Asset",
    other_liability: "Other Liability",
    credit_card: "Credit Card",
};

export const ASSET_CLASS_LABELS: Record<string, string> = {
    cash: "Cash",
    us_equity_large_cap: "US Equity Large Cap",
    us_equity_small_cap: "US Equity Small Cap",
    intl_equity: "International Equity",
    bonds: "Bonds",
    fixed_income: "Fixed Income",
    real_estate: "Real Estate",
    target_date: "Target Date",
    other: "Other",
    // The fixed class of every liability. It is not in ASSET_CLASSES, so no picker offers it.
    debt: "Debt",
};

// A prefill is the most recent value from before this month. The entry grid uses it for
// the "carried from 2026-06" text. A null means that no earlier value exists.
export interface NetWorthPrefill {
    value: number;
    fromYearMonth: string;
}

// One asset-class line within an account for a given month.
export interface NetWorthClassEntry {
    assetClass: string;
    value: number | null;
    prefill: NetWorthPrefill | null;
}

export interface NetWorthRow {
    accountId: string;
    note: string | null; // account-level, month-specific; never carried forward
    classes: NetWorthClassEntry[]; // active classes ∪ any class with a value this month
}

export interface NetWorthMonth {
    yearMonth: string;
    rows: NetWorthRow[];
}

export interface NetWorthTotals {
    assets: number;
    liabilities: number;
    netWorth: number;
}

// The Excel-style wide history from GET /api/net-worth/history.
export interface NetWorthHistory {
    accounts: Account[]; // active accounts + any with history, in sortOrder
    months: string[]; // every month with a value, sorted ascending (YYYY-MM)
    values: Record<string, Record<string, number>>; // { yearMonth: { accountId: value } }
    notes: Record<string, Record<string, string>>; // { yearMonth: { accountId: note } } — only cells with a note
    totals: Record<string, NetWorthTotals>; // { yearMonth: totals }
}
