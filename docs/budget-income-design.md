# Design - Budget Income and Left to Assign

This is the design for Specs 31 to 57 in `docs/budget-specs.md`. It is a proposal. The code does not
do any of this yet. When the work is complete, move the final design into
`docs/personal-finance-architecture.md`, and delete this file.

## Goal

The budget shows the **left to assign** amount for each year-month: the planned net income minus
the sum of the budget targets. The goal is `$0`. Every dollar of net pay then has a job (Specs 40
to 43).

Three changes make this possible:

1. **An income plan.** Income sources, a paycheck with gross pay and deductions, and one-time
   income lines. The income plan obeys the same month rules as a budget target (Specs 31 to 39).
2. **Transaction dispositions.** Budget, Off-budget, and Skip. A paycheck, a transfer, and a card
   payment then stay out of the actuals (Specs 45 to 51).
3. **A better CSV import.** Each upload has an account. The import suggests a disposition from the
   direction of the money. It sends less data to the LLM (Specs 44, 52 to 57).

## Spec Map

| Area | Specs | Section |
|---|---|---|
| Income sources and paychecks | 31 to 33, 36, 37 | Data Model, Income Plan API |
| Month rules for the income plan | 34, 39 | Income Plan Month Rules |
| One-time income lines | 35 | Data Model, Income Plan API |
| Income page | 38 | Frontend |
| Left to assign | 40 to 43 | Left to Assign |
| Accounts | 44 | Accounts |
| Dispositions and the note | 45 to 49 | Data Model, Transaction API |
| Summary changes | 50, 51 | Summary |
| CSV import | 52 to 57 | CSV Import |

## Data Model

Each amount is an integer in millionths of a dollar, the same as in the rest of the app.

### New table: `IncomeSource-{stage}`

One row for each income source (Spec 31). It holds only the data that does not change month to
month. The money is in `IncomePlan-{stage}`.

```
+---------------------+------------------------------------------------------------------+
| Field               | Notes                                                            |
+---------------------+------------------------------------------------------------------+
| incomeSourceId (PK) | ULID.                                                            |
| personId            | The person in the Profile table.                                 |
| label               | Free text, such as "Employer A". Not unique.                     |
| frequency           | "weekly" | "biweekly" | "semimonthly" | "monthly". Display only.   |
| active              | Soft delete, the same as Category (Spec 37).                     |
| sortOrder           | Display order on the Income page.                                |
| createdAt/updatedAt | ISO-8601 UTC.                                                    |
+---------------------+------------------------------------------------------------------+
```

The label is real data, so it lives only in DynamoDB. The code holds no employer name (Spec 31).

### New table: `IncomePlan-{stage}`

PK `yearMonth`, SK `lineId`. This table holds two kinds of row.

**A paycheck row.** `lineId = "SRC#{incomeSourceId}"`. One row for each income source in each
dense year-month. This row obeys the month rules of a Budget row (see **Income Plan Month Rules**).

```
{
  yearMonth: "2026-11",
  lineId: "SRC#01J...",
  lineType: "PAYCHECK",
  incomeSourceId: "01J...",
  paychecksPerMonth: 2,            // Spec 33
  gross: 5000000000,               // one paycheck
  deductions: [                    // one paycheck; order is the display order
    { label: "Federal tax", kind: "TAX",              amount: 600000000 },
    { label: "401k",        kind: "PRETAX_SAVINGS",   amount: 400000000 },
    { label: "DCFSA",       kind: "PRETAX_BENEFIT",   amount: 200000000 },
    { label: "Roth 401k",   kind: "POSTTAX_SAVINGS",  amount: 100000000 }
  ]
}
```

- The deduction kinds are `TAX`, `PRETAX_SAVINGS`, `PRETAX_BENEFIT`, and `POSTTAX_SAVINGS`
  (Spec 32). The kind is for display and for later features only. No calculation uses it.
- The app does not store the net pay. It calculates the net pay on each read:
  `net = gross − sum(deductions.amount)`, and `monthlyNet = net × paychecksPerMonth`.
- Validation: `gross >= 0`, each deduction `amount >= 0`, `net >= 0`, `paychecksPerMonth` is an
  integer from 1 to 5, and each label is not empty.
- The whole paycheck is one value. A change to one deduction writes the whole row again. This
  keeps the month rules simple: the walk-back copies one value, the same as for a Budget row.

**A one-time income line.** `lineId = "ONE#{ULID}"` (Spec 35).

```
{
  yearMonth: "2026-12",
  lineId: "ONE#01J...",
  lineType: "ONE_TIME",
  label: "Bonus",
  amount: 3000000000,              // net amount, > 0
  createdAt, updatedAt
}
```

A one-time line never takes part in the walk-back or the densify step. The code that builds the
walk-back map reads only `SRC#` rows.

**Why a new table, and not more rows in `Budget-{stage}`?** The Budget SK is a `categoryId`, and the
`/replace` body rule, the summary, the hard delete, and the densify step all read every row of a
month as a category row. A separate table keeps each of these as it is.

### Changed table: `Transactions-{stage}`

New fields. The keys do not change.

```
+--------------+------------------------------------------------------------------------+
| Field        | Notes                                                                  |
+--------------+------------------------------------------------------------------------+
| disposition  | "BUDGET" | "OFF_BUDGET" | "SKIP". Absent means "BUDGET" (Spec 45).     |
| categoryId   | Required when the disposition is BUDGET. Optional for OFF_BUDGET.      |
|              | Absent for SKIP.                                                       |
| accountId    | The account of the upload (Spec 44). Optional on a manual transaction. |
|              | Absent on every transaction that exists today.                         |
| note         | Optional free text, 200 characters at most (Spec 47).                  |
| fingerprint  | Set by the CSV import only. See **Import fingerprints**.               |
+--------------+------------------------------------------------------------------------+
```

**No migration is necessary.** An absent `disposition` reads as `BUDGET`. Every row that exists
today has a `categoryId`, so every row that exists today stays valid. The code reads the
disposition through one helper, `disposition_of(txn)`, so the default lives in one place.

An existing paycheck in an old "Income" category stays a BUDGET row. The user can change it to
SKIP in an editable month. A locked month stays as it is, unless the user applies the override
(Spec 2).

The hard delete of a category (Spec 16) deletes every transaction with that `categoryId`, as
today. An OFF_BUDGET row with that category is deleted too. A SKIP row has no category, so the hard
delete never touches it.

### Changed table: `Account-{stage}` (the net worth record)

New optional fields:

```
+-------------+-----------------------------------------------------------------------+
| Field       | Notes                                                                 |
+-------------+-----------------------------------------------------------------------+
| budgetRole  | "SPENDING" | "HOLDING". Absent means the default for the account type. |
| lastFour    | Optional list of strings of 4 digits. The import uses them to find   |
|             | the other side of a transfer (see **CSV Import**, step 2).            |
+-------------+-----------------------------------------------------------------------+
```

The default role for each account type:

| Account type | Default budget role |
|---|---|
| `checking`, `credit_card` (new) | SPENDING |
| `savings`, `brokerage`, `529`, `530a` | HOLDING |
| All other types | none. The account cannot take a CSV upload until the user sets a role. |

**A new account type is necessary: `credit_card`.** The net worth record has no credit card type
today. A credit card is the main upload source, and Spec 44 needs an account for each upload. The
new type has `liability=True` and the fixed asset class `cash`, the same as `checking` but as a
liability. The net worth record can show a credit card balance too, but it does not have to: the
user can set `excludedFromNetWorth` on it.

### Changed table: `BudgetAuditLog-{stage}`

New actions for the income plan (Spec 39). An income entry has `incomeLineId` (the `lineId`) and,
for a paycheck, `incomeSourceId`. It has no `categoryId`.

| Action | Made by | `changes` |
|---|---|---|
| `INCOME_CREATE` | `/replace` writes a new paycheck row; the create of an income source | `{paycheck: {before: null, after: P}}` |
| `INCOME_UPDATE` | `/replace` changes a paycheck row | `{paycheck: {before: P, after: P}}` |
| `INCOME_PIN` | `/pin` writes a future paycheck pin | `{paycheck: {before: P \| null, after: P}}` |
| `INCOME_UNPIN` | `/pin` with `paycheck: null`; a deactivation that drops a pin | `{paycheck: {before: P, after: null}}` |
| `ONE_TIME_CREATE` | a one-time line is added | `{line: {before: null, after: L}}` |
| `ONE_TIME_UPDATE` | a one-time line changes | `{line: {before: L, after: L}}` |
| `ONE_TIME_DELETE` | a one-time line is deleted | `{line: {before: L, after: null}}` |

`P` is `{paychecksPerMonth, gross, deductions}`. `L` is `{label, amount}`. The audit log page shows
the label of the income source, or the label of the one-time line, where it shows a category name
today. The rename of an income source and its reactivation make no entry, the same as for a
category.

## Income Plan Month Rules

The income plan obeys every month rule of the Budget table (Spec 34). The rules are in
**Carry-Forward** in the architecture doc. In short:

- The current month, the grace month, and every locked month are **dense**: one paycheck row for
  each income source that was active when the month became dense.
- A future month is **sparse**: only the paycheck rows that the user pinned.
- A future month resolves each active income source: a pin, or else a walk-back to the most recent
  earlier row.
- The densify step fills each missing month, oldest first, for each active income source. It
  leaves a pin in place. It never copies a `ONE#` row.

**Refactor `budget/densify.py` to serve both tables.** Today `densify()` and
`resolve_future_targets()` take a `BudgetTable` and a `CategoryTable`. Change them to take a small
protocol:

```python
class DenseSparseStore(Protocol):
    def scan_carry_rows(self) -> list[tuple[str, str, Any]]: ...   # (yearMonth, key, value)
    def put_carry_row(self, year_month: str, key: str, value: Any) -> None: ...

def densify(store: DenseSparseStore, active_keys: list[str], current_ym: str, cold_start_value: Any) -> int: ...
def resolve_future(store: DenseSparseStore, active_keys: list[str], year_month: str) -> list[ResolvedRow]: ...
```

`BudgetTable` and `IncomePlanTable` each implement it. For the Budget table the key is the
`categoryId`, and the value is the amount. For the income plan the key is the `incomeSourceId`, and
the value is the paycheck `P`. The existing tests for the densify step must pass with no change to
their expected results.

Other rules:

- **Cold start.** If the income plan has no rows, the densify step writes nothing. An income source
  always has a paycheck row from its create (see below), so the empty case means "no income
  sources yet".
- **No earlier row.** The walk-back finds no row only for an income source with no history. That
  cannot happen through the API. If it does happen, the densify step writes no row, and the source
  adds `$0`.
- **The scheduled densify Lambda** (`scheduled_densify.py`) densifies both tables. The on-demand
  fallback runs for both tables on the same requests as today, and also on every income plan
  request.
- **Editability** is the same function. The Enforcement table gets the new endpoints (see
  **Income Plan API**).

## Income Plan API

All routes need a JWT, and all sit under `/api`, the same as today.

```
+--------+--------------------------------------------+----------------------------------------+
| Method | Path                                       | Purpose                                |
+--------+--------------------------------------------+----------------------------------------+
| GET    | /api/income-sources?include_inactive=true  | List income sources.                   |
| POST   | /api/income-sources                        | Create. Body: personId, label,         |
|        |                                            | frequency, initialPaycheck (P),        |
|        |                                            | explanation. Densify first, then write |
|        |                                            | the paycheck row at current_ym. One    |
|        |                                            | INCOME_CREATE entry.                   |
| PUT    | /api/income-sources/{id}                   | Change label or frequency. No          |
|        |                                            | editability check. No audit entry.     |
| POST   | /api/income-sources/{id}/deactivate        | Same flow as a category: returns       |
|        |                                            | affectedMonths with future pins; 409   |
|        |                                            | without confirm; with confirm, drops   |
|        |                                            | the pins and writes INCOME_UNPIN.      |
| POST   | /api/income-sources/{id}/reactivate        | Same as a category.                    |
| GET    | /api/income/{YYYY-MM}                      | The income plan of a month. See below. |
| POST   | /api/income/{YYYY-MM}/replace              | Dense-month write of paycheck rows.    |
| POST   | /api/income/{YYYY-MM}/pin                  | Future-month write of paycheck pins.   |
| POST   | /api/income/{YYYY-MM}/one-time             | Create a one-time line.                |
| POST   | /api/income/{YYYY-MM}/one-time/update      | Change a one-time line.                |
| POST   | /api/income/{YYYY-MM}/one-time/delete      | Delete a one-time line.                |
+--------+--------------------------------------------+----------------------------------------+
```

There is no hard delete for an income source. A mistake is fixed by a deactivation. If a hard
delete becomes necessary later, it copies the category hard delete.

**`GET /api/income/{YYYY-MM}`** returns:

```
{
  "yearMonth": "2026-11",
  "state": "EDITABLE" | "GRACE" | "LOCKED",
  "paychecks": [
    {
      "incomeSourceId", "label", "personId",
      "paychecksPerMonth", "gross", "deductions": [...],
      "net",            // one paycheck
      "monthlyNet",     // net × paychecksPerMonth
      "pinned": bool    // future months only, the same as a budget row
    }, ...
  ],
  "oneTime": [{ "lineId", "label", "amount" }, ...],
  "plannedNetIncome": number
}
```

Membership is the same as Spec 5. An editable month shows the active income sources. A locked
month shows every income source with a paycheck row in that month, active or not.

**`/replace`** and **`/pin`** copy the Budget endpoints exactly. The body rows are
`{incomeSourceId, paycheck: P | null}`. `/replace` needs every income source with a row in that
month. `/pin` returns `downstreamPins` and `pinMatchesCarriedValue`. Two paychecks match when
`paychecksPerMonth`, `gross`, and the full deduction list are equal. Each body has an
`explanation` and an optional `override`.

**The one-time routes.** The year-month must be EDITABLE or GRACE, or LOCKED with the override.
This includes a future month. Each body has an `explanation`. `amount` must be more than 0.

## Left to Assign

```
plannedNetIncome(M) = sum(monthlyNet of each paycheck row in M that is a member of M)
                    + sum(amount of each ONE# row in M)
leftToAssign(M)     = plannedNetIncome(M) − totals.budgeted(M)
```

- `totals.budgeted(M)` is the existing value from `compute_summary()`. The two numbers therefore
  use the same category membership (Spec 5).
- For a future month, the paycheck rows come from the walk-back, the same as the targets.
- A locked month uses its own dense rows only, so the value never changes after the lock (Spec
  42).
- No transaction changes either number (Spec 43).

New shared function: `compute_income(income_plan, income_sources, year_month, now)` in
`aws/dynamodb.py`, next to `compute_summary()`. It returns the body of `GET /api/income/{YYYY-MM}`.

The value appears in three responses:

- `GET /api/summary` gets an `income` object: `{plannedNetIncome, leftToAssign}`.
- `GET /api/budget/{YYYY-MM}` gets `plannedNetIncome`. The budget editor calculates
  `leftToAssign` on the client, live, from the amounts in the form. The user then sees the number
  move while they type (Spec 41).
- `GET /api/income/{YYYY-MM}` has `plannedNetIncome`.

The app never blocks a save because `leftToAssign != 0` (Spec 41). The frontend shows the value in
a different colour when it is not `$0`.

## Summary

Changes to `compute_summary()`:

1. **Only BUDGET rows are actuals** (Spec 50). The loop over the transactions reads
   `disposition_of(txn)`. A BUDGET row adds to its category. An OFF_BUDGET row adds to
   `offBudget`. A SKIP row adds to nothing.
2. **Category membership** (Spec 5) looks at BUDGET rows only. An OFF_BUDGET row with a category
   does not put that category on the summary.
3. **New fields in the response** (Spec 51):

```
"totals":     { "budgeted", "actual", "delta" },        // as today, BUDGET rows only
"offBudget":  { "actual": number },                      // budgeted is always 0
"totalsWithOffBudget": { "budgeted", "actual" },         // budgeted = totals.budgeted
"income":     { "plannedNetIncome", "leftToAssign" }
```

The frontend draws the Off-budget row and the "TOTAL + off-budget" row below the TOTAL row.

## Accounts

The user chooses an account for each upload (Spec 44). The upload page lists each active account
with a budget role, explicit or default. The user sets `budgetRole` and `lastFour` on the account
page of the net worth record, through the existing account update endpoint. Add both fields to
the list of fields that the update permits.

## Transaction API

Changes to the existing routes:

- `POST /api/transactions`, `POST /api/transactions/update`, and `POST /api/transactions/commit`
  accept `disposition`, `accountId`, and `note`.
- Validation:
  - `disposition = BUDGET` needs a `categoryId`.
  - `disposition = SKIP` must have no `categoryId`. The server removes it if the client sends one.
  - `accountId`, if present, must be an account in the Account table.
  - `note`, if present, is at most 200 characters after a trim. An empty note is removed.
- A change of disposition, category, or note obeys the same editability rules as today (Spec 48).
- `GET /api/transactions?month=YYYY-MM` returns every row, with `disposition`, `accountId`, and
  `note`. The ledger page hides SKIP rows on the client by default (Spec 49).

## CSV Import

### Overview

```
upload (csvData, accountId)
  1. parse the CSV
  2. find the columns           -> LLM call 1 (header row + local column types; no values)
  3. normalize each row          -> date, description, signed amount, bank category (if any)
  4. fingerprint each row        -> mark rows that exist already
  5. find the other side         -> pair match, last four, bank category
  6. classify each description   -> LLM call 2 (unique descriptions only)
  7. suggest a disposition       -> the direction table
  8. return rows for review
commit (rows)
  9. drop rows with a fingerprint that exists; write the rest
```

The order of steps 5 and 6 does not matter for correctness. Step 6 runs for every unique
description, because a BUDGET row needs a category in any case. Step 7 then uses all the
information together.

### Step 2: column mapping with no real values (Spec 57)

The code finds a type for each column locally: `date` (most values parse as a date), `number`
(most values parse as an amount), or `text`. It sends the header row and the types to the LLM. It
sends no cell values.

```
Headers and types:
Transaction Date (date), Post Date (date), Description (text), Category (text), Type (text), Amount (number)
```

The LLM returns `date`, `description`, `amount` (or a `debit` and a `credit` column), `bankCategory`
(optional, new), and `invertSign`.

**A risk to test.** The sign rule is the hard part. Today the LLM sees sample amounts and can tell
whether a purchase is negative. Without values, it must use the header names. Mitigation: the code
can find the sign locally. In a credit card CSV most rows are purchases. In a checking CSV most rows
are debits. So, in each file, the sign of the larger number of rows is "money out", unless the
role of the account says otherwise. The review screen shows the result, and the user can flip the
sign of the whole file there. Test this against real bank CSVs before the build.

### Step 3: the bank category

If the mapping has a `bankCategory` column, the code keeps its value on each row. A small,
generic list of words in the code turns it into a hint:

| The bank category contains | Hint |
|---|---|
| "payroll", "paycheck", "salary", "direct deposit", "income", "interest", "dividend" | OUTSIDE (income) |
| "transfer" | OWN_ACCOUNT |
| "credit card payment", "card payment", "payment" (on a card CSV, money in) | OWN_ACCOUNT, card payment |

These words are generic bank labels. They are not employer names or other real data. The bank
category itself is never sent to the LLM.

### Step 4: import fingerprints (Spec 56)

```
fingerprint = sha256(accountId | transactionDate | amount | normalizedDescription | n)
```

- `normalizedDescription` is the description in upper case, with each run of white space
  replaced by one space.
- `n` is the count of earlier rows in the same file with the same first four values. Two real
  coffees on the same day for the same amount then get different fingerprints.
- The upload loads the transactions of each year-month in the file (one Query for each month), and
  marks each row whose fingerprint exists already: `duplicate: true`. The review screen leaves
  these rows unselected.
- The commit does the same check again, just before it writes. A retry after a `500` then writes
  only the rows that are not there yet. This removes the trade-off "a retry is not idempotent" in
  the architecture doc, for CSV rows. Two commits at the same time can still both write. That is
  acceptable for one household.
- A row from before this change has no fingerprint, so it never matches. The first upload of a
  date range that overlaps an old upload can therefore make a copy. The pair match does not help
  here. The user must look for this one time, at the first upload.

### Step 5: find the other side of each row

The amount after normalization is positive for money out and negative for money in, from the
point of view of the upload account. The open question is the other side: the outside world, or
one of my accounts. The steps below run in order. The first step that gives an answer wins.

1. **Pair match** (Spec 55). For each row, look for a transaction with the opposite sign, the
   same absolute amount, a date at most 4 days away, and a different `accountId`. Look in the same
   upload and in the saved transactions of the year-months in range (any disposition). If exactly
   one candidate is found, the other side is that account, with its role. If more than one is
   found, mark the row, and do not choose.
2. **Last four digits.** If the description contains a 4-digit group that is in the `lastFour` of
   exactly one other account, the other side is that account.
3. **Bank category hint**, from step 3.
4. **The LLM kind**, from step 6.
5. **Unknown.**

Each row gets `suggestedBy`: `PAIR`, `LAST_FOUR`, `BANK`, `LLM`, or `DEFAULT`.

### Step 6: classify each description

The existing categorization call changes in two ways:

- It sends each **unique** description one time only (Spec 57). The code maps the answer back to
  every row with that description.
- It returns a `kind` as well as a category:

```
[{"index": 0, "kind": "expense" | "refund" | "income" | "card_payment" | "own_transfer",
  "categoryId": "..." | null}]
```

For `own_transfer`, the LLM may suggest a category, such as a savings category. For `income` and
`card_payment`, it returns `null`. The prompt still gets only the descriptions and the category
names. No amount, no date, and no account name goes to the LLM.

### Step 7: suggest a disposition (Spec 52)

Inputs: the role of the upload account, the direction (out or in), and the other side from step 5.

| Upload role | Money | Other side | Suggested disposition | Mark for review? |
|---|---|---|---|---|
| SPENDING | out | outside | BUDGET, LLM category | only if no category |
| SPENDING | out | SPENDING account (pair, last four) | SKIP | no |
| SPENDING | out | HOLDING account (pair, last four) | BUDGET, LLM category | yes, if the LLM gave no category |
| SPENDING | out | own account, role unknown, LLM says `card_payment` | SKIP | no |
| SPENDING | out | own account, role unknown, LLM says `own_transfer` with a category | BUDGET, that category | **yes** |
| SPENDING | out | own account, role unknown, no category | SKIP | **yes** |
| SPENDING | in | outside, LLM says `refund` | BUDGET, LLM category | only if no category |
| SPENDING | in | outside, income | SKIP | no |
| SPENDING | in | any own account | SKIP | no |
| HOLDING | out | outside | OFF_BUDGET | no |
| HOLDING | out | any own account | SKIP | no |
| HOLDING | in | anything | SKIP | no |
| any | any | unknown (no step gave an answer) | SKIP | **yes** |

The principle: when the import is not sure, it suggests SKIP and marks the row. A missed savings
transfer shows as a `$0` actual in a savings category, and the user sees it. A double count is hard
to see.

### Upload response

The row gets new fields. `validation.issues` keeps its meaning: an issue blocks the commit. The new
`review` list does not block the commit. It only puts the row at the top of the review screen.

```
{
  "accountId": "01J...",
  "rows": [
    {
      "transactionDate", "description", "amount",
      "disposition": "BUDGET" | "OFF_BUDGET" | "SKIP",
      "categoryId": "01HQ..." | null,
      "suggestedBy": "PAIR" | "LAST_FOUR" | "BANK" | "LLM" | "DEFAULT",
      "pairWith": { "accountId", "yearMonth", "sortId" } | { "uploadIndex" } | null,
      "fingerprint": "…",
      "duplicate": bool,
      "review": ["no_pair_found", "llm_guess", "many_pairs"],
      "validation": {
        "editability": "EDITABLE" | "GRACE" | "LOCKED",
        "issues": ["missing_category", "locked_month"]
      }
    }
  ]
}
```

`missing_category` now applies to a BUDGET row only.

### Commit

The commit body rows get `disposition`, `accountId`, `note`, and `fingerprint`. The client sends
only the rows that the user selected. The server does these steps:

1. Validate each row (the Transaction API rules above).
2. Run the editability check, as today.
3. Load the year-months of the batch, and drop each row whose fingerprint exists. The response
   lists them as `skippedDuplicates`.
4. Write the rest with `BatchWriteItem`, as today.

## Frontend

- **`IncomePage`** (new, Spec 38). A month picker. For each income source: the paycheck as a list,
  from gross pay through each deduction to net pay, then `× paychecksPerMonth = monthlyNet`. The
  one-time lines of the month. The planned net income at the bottom. The edit flow copies the
  budget editor: `/replace` for a dense month, `/pin` for a future month, the same pin chip, the
  same downstream-pin warnings, and an explanation box. A section to add, rename, deactivate, and
  reactivate an income source.
- **`BudgetPage`.** A header line: "Planned net income $X · Left to assign $Y". The value of `Y`
  changes live while the user edits. It has a different colour when it is not `$0`.
- **`SummaryPage`.** The same header line. Below the TOTAL row: the Off-budget row, then the
  "TOTAL + off-budget" row.
- **`TransactionsPage`.** New columns: disposition, account, and note. A filter "Show skipped
  rows", off by default. An edit of a row can change the disposition and the note.
- **`UploadPage`.** An account picker before the upload (required). Rows marked for review show
  first, with the reason. Then BUDGET rows. Then OFF_BUDGET and SKIP rows in a separate list
  (Spec 54). Each row has a disposition picker, a category picker (shown for BUDGET and
  OFF_BUDGET), and a note field. Duplicate rows show unselected, with a "duplicate" chip. A
  "flip all signs" button for the whole file (see Step 2).
- **The account page of the net worth record.** A budget role picker and a "last four digits"
  field.
- **`AuditLogPage`.** It shows the new actions, with the label of the income source or the
  one-time line.

## Infrastructure

`PersonalFinanceDynamoDbStack`:

- Add `IncomeSource-{stage}` (PK `incomeSourceId`) and `IncomePlan-{stage}` (PK `yearMonth`, SK
  `lineId`). Pay per request, point-in-time recovery, and the same removal policy as the other
  tables.
- Change the JSDoc of the stack class to list the two new tables.

`PersonalFinanceWebStack`, and the stack of the scheduled densify Lambda:

- New environment variables: `INCOME_SOURCE_TABLE_NAME` and `INCOME_PLAN_TABLE_NAME`. Read them
  with `get_env_var()`.
- Give the handler Lambda read and write access to both tables.
- Give the scheduled densify Lambda read access to `IncomeSource` and read and write access to
  `IncomePlan`.

No new GSI is necessary. Each new query reads one year-month partition, or scans a small table.

## Testing

New and changed tests in `lambda/tests/`:

- `test_budget_densify.py` — the existing cases still pass through the new protocol. New cases for
  the income plan: walk-forward, pins, and a one-time line that never carries forward.
- `test_budget_income.py` (new) — the net pay calculation and its validation, the income source
  life cycle, `/replace`, `/pin`, the one-time routes, the audit entries, the editability checks,
  and `compute_income()` for editable, future, and locked months.
- `test_budget_summary.py` — dispositions (BUDGET only in categories and membership), the
  Off-budget total, `totalsWithOffBudget`, and `leftToAssign`.
- `test_budget_transactions.py` — disposition validation, the note, and the account.
- `test_budget_csv_import.py` — the local column types, the prompt without values, unique
  descriptions, the `kind` in the answer, fingerprints with `n`, the pair match (none, one, many),
  last four, the bank hint, and every row of the direction table.
- `infra/test/` — the two new tables, and the new environment variables and grants.

## Order of Work

Each step can ship alone, and each step leaves the app in a working state.

1. **Dispositions and the note.** The Transactions fields, the API validation, the summary
   (BUDGET only, the Off-budget rows), and the ledger page. The user can mark work travel at once.
2. **Income plan.** The two tables, the densify refactor, the income API, `compute_income()`, the
   audit actions, and the Income page.
3. **Left to assign.** The new fields on the summary and budget responses, and the header lines.
   This step delivers the main goal (Spec 40).
4. **Accounts.** The `credit_card` account type, `budgetRole`, `lastFour`, and the account picker
   on the upload page.
5. **CSV import.** Fingerprints, the column mapping without values, unique descriptions, the
   `kind`, the other side, the direction table, and the new review screen.

## Open Items

- **The `credit_card` account type** changes the net worth record. Confirm that a credit card in
  the Account table is acceptable, and whether it shows in net worth.
- **The column mapping without values** (Step 2) can be wrong about the sign. Test it with real
  CSVs from each bank before step 5 of the work.
- **The first upload after the change** can make copies of old rows, because old rows have no
  fingerprint (Step 4).
- **The definition of an income source.** The user wants to read it again (Specs 31 and 37).
