# Design - Budget Income and Left to Assign

This is the design for Specs 31 to 57 in `docs/budget-specs.md`. It is a proposal. The code does not
do any of this yet. When the work is complete, move the final design into
`docs/personal-finance-architecture.md`, and delete this file.

Read these documents in this order:

1. `docs/budget-specs.md` — what the user wants. Specs 31 to 57 are new.
2. This document — how to build it. Read **Decisions and Reasons** before you change a decision.
3. `docs/budget-income-workplan.md` — the checkpoints, the files for each one, and the progress.
4. `docs/personal-finance-architecture.md` — how the app works today. The month rules, the
   editability rules, and the audit rules there apply to the new work too.

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

## Decisions and Reasons

The user made these decisions in a design chat. Do not change one without a question to the user.

| Decision | Reason |
|---|---|
| The budget is **plan-based**, not cash-based. Left to assign = planned net income − targets. | The user wants a stable monthly plan, like EveryDollar or the tracking mode of Actual Budget. A cash-based plan (YNAB) needs work on each payday. |
| The plan uses **2 paychecks for each source, each month**, all year. | Two months each year have 3 biweekly paychecks. The user wants the same targets every month. The plan is then a little lower than the real income. The user accepts that error. |
| The **third paycheck**, bonuses, RSU payouts, and DCFSA payouts are **not in the plan** unless the user adds a one-time income line. | The Ramsey way: extra money that the user does not plan for does not exist. The app shows no surplus and gives no warning. |
| **Record gross pay and each deduction.** Net pay is the top line. | The user wants to see why net pay is the number that it is. |
| **Do not model changes to net pay during the year** (the 401k limit, the Social Security wage limit). | No budget app does this. The user cannot keep it correct. The user changes the paycheck by hand. |
| **DCFSA is only a pre-tax deduction.** It has no link to a category. | The user can use DCFSA money for any qualified expense, not only childcare. A DCFSA payout is extra money, the same as the third paycheck. |
| The income plan uses the **same month rules as a budget target**. | Consistency. A locked month must show the same left to assign amount for ever (Spec 2). |
| The unit of income is an **income source**, not a person or a job. An income source is any stream of income, such as a job, interest, or dividends. | A person can have more than one job, and later the user may plan interest or dividend streams. Each kind works the same way. |
| **Actual income is not tracked.** | The user does not want it. Paycheck deposits get Skip. |
| **No category balances** (no envelopes, no carryover). | The user does not want them. Money counts one time, when it goes into a fund. Money that leaves a fund is Off-budget. |
| **A target that carries forward after a one-month change is OK.** | The user changes it back by hand. |
| **One disposition, "Off-budget"**, for work travel and for payments from a fund. | One idea for the user: "the budget must not count this". The optional note keeps the reason. |
| **The app saves every row**, Skip rows too. | Fingerprints and the pair match need them. The user can change a row later. |
| No "income" disposition. Skip covers paychecks and transfers. | Actual income is not tracked, so a separate value adds nothing. |
| The Off-budget row is **below the TOTAL, not in it**. A second "TOTAL + off-budget" line follows. | If it were in the TOTAL, the total delta would always show an overspend that is not real. |
| **Each upload has an account.** The boundary rule (spending vs. holding) sets the default disposition. | Each transfer between the user's own accounts then counts one time at most, with no search. |
| **When unsure, suggest Skip and mark the row.** | A missed savings transfer is easy to see on the summary. A double count is not. Human review is the backstop, so the import balances effort against errors. |
| **Send as little data to Anthropic as possible**: no cell values for the column mapping, unique descriptions only. | The user's choice. |
| **No employer names or other real data in the code.** | The repository is public. Real data lives only in DynamoDB. |
| **Import rules, remembered choices, and a stored column mapping are deferred.** | They fit in later as one more step before the LLM step. Nothing in this design blocks them. |
| Left to assign **does not block a save**. The ledger **hides Skip rows by default**. A row that exists already is **not selected by default**. An income source can be **deactivated**. | The user confirmed these four assumptions (Specs 41, 49, 56, 37). |

## Code Map

The files that the work changes. Paths are from the repository root.

| Area | Files |
|---|---|
| HTTP routes | `lambda/src/corderohq/personal_finance_handler.py` (one `if`/`elif` chain in `handler()`; module-level table objects at the top; `_ensure_hydrated()` runs the densify step) |
| Table classes, summary | `lambda/src/corderohq/aws/dynamodb/`: one file for each table, with a file name that matches the table (`budget.py`, `transactions.py`, `budget_audit_log.py` with `BudgetAuditLogTable`, `account.py`, and so on), `summary.py` (`compute_summary()`), `income_summary.py` (`compute_income()`), `util.py` (the shared resource and the paging helpers). Import every name from `corderohq.aws.dynamodb`. |
| Month rules | `lambda/src/corderohq/budget/densify.py` (`DenseSparseStore`, `densify()`, `resolve_future()`, `resolve_future_targets()`, `walk_back_for()`, `value_at_or_before()`), `budget/editability.py`; the adapter `BudgetCarryStore` in `aws/dynamodb/budget.py` and `active_category_ids()` in `aws/dynamodb/category.py` |
| CSV import | `lambda/src/corderohq/budget/csv_import/`: `llm.py` (`map_columns()`, `categorize_transactions()`), `local_checks.py`, `fingerprints.py`, `directions.py`; the upload flow is `_post_transactions_upload()` in the handler |
| Cron densify | `lambda/src/corderohq/scheduled_densify.py` |
| Account types | `lambda/src/corderohq/networth/models.py` (`AccountType`, `_ACCOUNT_TYPE_META`) |
| Tables (CDK) | `infra/lib/personal-finance-dynamodb-stack.ts` |
| Lambda env vars and grants (CDK) | `infra/lib/personal-finance-web-stack.ts` (the API handler and `densifyFn`); `infra/app.ts` passes the tables between the stacks |
| Frontend types and parsers | `web/personal-finance/src/types.ts`, `parsers.ts`, `api.ts` |
| Frontend pages and nav | `web/personal-finance/src/pages/*.tsx`, `App.tsx` (routes), `components/Layout.tsx` (`BUDGET_LINKS`) |
| Tests | `lambda/tests/test_budget_*.py`, `test_personal_finance_handler.py`, `test_networth_models.py`; `infra/test/*.test.ts`. The Python tests use `MagicMock` tables, not a real DynamoDB. |

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

`TransactionsTable.update()` today only uses `SET`, for `description`, `amount`, and `categoryId`.
It must also `SET` the new fields, and `REMOVE` `categoryId` or `note` when the new value is absent
(a change to SKIP, or an empty note). `create()` and `batch_create()` must store the new fields.

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

**A new account type, `credit_card`, and a new asset class, `debt` (decided 2026-10-17).**

- The net worth record has no credit card type today. The user's existing card is an
  `other_liability` account. A credit card is the main upload source, and Spec 44 needs an
  account for each upload.
- `credit_card`: `liability=True`, fixed asset class `debt`, `amortizing=False` (no loan terms).
- **`debt`** is a new asset class for every liability. `mortgage` and `other_liability` change
  their fixed asset class from `other` to `debt`. The existing data changes too (see the
  migration below).
- `debt` is only a fixed class. An asset account can never choose it. The class pickers on
  `AccountsPage.tsx` and `NetWorthEntryPage.tsx` (`ADDABLE_CLASSES`) must leave it out, and the
  backend must reject it for a type that lets the user choose classes.
- Add the type and the class in the places that must match:
  - `networth/models.py`: `AccountType`, `AssetClass`, and `_ACCOUNT_TYPE_META`.
  - `web/personal-finance/src/types.ts`: `ACCOUNT_TYPES`, `ACCOUNT_TYPE_META`,
    `ACCOUNT_TYPE_LABELS`, `ASSET_CLASSES`, and `ASSET_CLASS_LABELS`.
- The net worth record can show a credit card balance, but it does not have to: the user can set
  `excludedFromNetWorth` on it.

**One-time migration script: `lambda/scripts/migrate_liabilities_to_debt.py`.** Copy the style of
`migrate_networth_asset_classes.py`: `--stage`, a dry run by default, `--apply` to write, and safe
to run again.

1. Each `Account` row with `accountType` `mortgage` or `other_liability`: change `assetClasses`
   from `["other"]` to `["debt"]`.
2. Each `NetWorthSnapshot` row of those accounts: move the value at `byAssetClass.other` to
   `byAssetClass.debt`.
3. Each account ID that the user gives with `--to-credit-card <accountId>`: change `accountType`
   to `credit_card`, set `assetClasses` to `["debt"]`, and remove `loanTerms`. Its snapshot rows
   move from `other` to `debt` in step 2, because it is an `other_liability` account before the
   change.
4. Print each change. Run it on dev first, then on prod.

Also check `lambda/scripts/import_networth_csv.py`. Its comments and its class map name `other`
for a mortgage.

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
today. In `types.ts`, `AuditEntry.categoryId` becomes optional, and `incomeSourceId` and
`incomeLineId` are new optional fields. For a one-time line, store the label in `changes`, because
the line can be deleted later. The rename of an income source and its reactivation make no entry, the same as for a
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

**`budget/densify.py` serves both tables (done in CP2).** The module does not import any table
class. It works on a small protocol, and each caller passes the list of active keys:

```python
CarryRow = tuple[str, str, Any]   # (yearMonth, key, value)

class DenseSparseStore(Protocol):
    def scan_carry_rows(self) -> list[CarryRow]: ...
    def put_carry_row(self, year_month: str, key: str, value: Any) -> None: ...

def densify(store, active_keys, current_ym, cold_start_value) -> int: ...
def resolve_future(store, active_keys, year_month) -> list[ResolvedValue]: ...   # key, value, pinned
def resolve_future_targets(budget_store, active_category_ids, year_month) -> list[dict]: ...  # API shape
def walk_back_for(by_key, target, key) -> Any: ...          # starts at the month before `target`
def value_at_or_before(by_key, year_month, key) -> Any: ... # the row in `year_month`, else walk back
```

- For the Budget table, `BudgetCarryStore(budget_table)` in `aws/dynamodb/budget.py` is the adapter. The
  key is the `categoryId`, and the value is the amount. It reads through `scan_all()` and writes
  through `put_single()`. It is an adapter, and not methods on `BudgetTable`, so that the handler
  tests, which replace `BudgetTable` with a mock, still work.
- `active_category_ids(_CATEGORY_TABLE.list_all())` gives the active keys. It filters in Python,
  the same as the old code.
- `compute_summary()` now uses `value_at_or_before()`. Its own copy of the walk-back code
  (`_walk_back_amount()`) is gone. The copy existed only because `densify.py` imported
  `aws/dynamodb.py`, and that import is gone.
- **For CP3:** make `IncomePlanCarryStore(income_plan_table)` the same way. The key is the
  `incomeSourceId`, and the value is the paycheck `P`. It must read only `SRC#` rows, and strip the
  `SRC#` prefix from the key.

Other rules:

- **Cold start.** If the income plan has no rows, the densify step writes nothing. An income source
  always has a paycheck row from its create (see below), so the empty case means "no income
  sources yet".
- **No earlier row.** The walk-back finds no row only for an income source with no history. That
  cannot happen through the API. If it does happen, the densify step writes no row, and the source
  adds `$0`.
- **The scheduled densify Lambda** (`scheduled_densify.py`) densifies both tables. The on-demand
  fallback is `_ensure_hydrated()` in the handler. It densifies both tables, on the same requests
  as today, and also on every income plan request.
- **Editability** is the same function. The Enforcement table gets the new endpoints (see
  **Income Plan API**).

## Income Plan API

All routes need a JWT, and all sit under `/api`, the same as today.

```
+--------+--------------------------------------------+----------------------------------------+
| Method | Path                                       | Purpose                                |
+--------+--------------------------------------------+----------------------------------------+
| GET    | /api/income-sources?include_inactive=true  | List income sources.                   |
| POST   | /api/income-sources/reorder                | Body: {order: [incomeSourceId]}.       |
|        |                                            | Writes sortOrder = index.              |
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

**`/replace`** and **`/pin`** copy the Budget endpoints. The budget endpoints take the body key
`targets`. The income endpoints take the body key `paychecks`, a list of
`{incomeSourceId, paycheck}`. Each income source in the body must exist. Each body has an
`explanation`, and `/replace` also takes an optional `override`.

- `/replace` needs every income source with a row in that month. **A `null` paycheck is not
  permitted here** (a `400`). To stop a source, deactivate it. Each change writes one
  INCOME_CREATE or INCOME_UPDATE entry. The response is the month, the same as
  `GET /api/income/{YYYY-MM}`.
- `/pin` takes `null` to remove a pin. It returns `{income: <the month>, warnings:
  {downstreamPins: [{incomeSourceId, yearMonth}], pinMatchesCarriedValue: [incomeSourceId]}}`.
- Two paychecks match when `paychecksPerMonth`, `gross`, and the full deduction list are equal,
  after each goes through the checks (so a stored `Decimal` equals an `int`). The same paycheck
  again is a no-op with no audit entry.

**The one-time routes.** The year-month must be EDITABLE or GRACE, or LOCKED with the override.
This includes a future month. Each body has an `explanation`. `amount` must be more than 0.
`/one-time/update` and `/one-time/delete` take a `lineId` that starts with `ONE#`.

**API Gateway.** API Gateway lists each route explicitly (`createHttpApi()` in
`personal-finance-web-stack.ts`). Each new route must be in that list, or API Gateway returns a
404 before the Lambda sees the request.

**The code.** `budget/income.py` holds the paycheck rules (`validate_paycheck()`, `net_pay()`,
`monthly_net()`, `paychecks_equal()`) and the row IDs. `aws/dynamodb/` holds
`IncomeSourceTable`, `IncomePlanTable`, the adapter `IncomePlanCarryStore`,
`active_income_source_ids()`, and `compute_income()`. Each paycheck entry in the response also
has `active`, so the Income page can mark an inactive source in a locked month.

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
`aws/dynamodb/income_summary.py`. It returns the body of `GET /api/income/{YYYY-MM}`.

The value appears in two responses:

- `GET /api/summary` gets an `income` object: `{plannedNetIncome, leftToAssign}`.
- `GET /api/income/{YYYY-MM}` has `plannedNetIncome`.

`GET /api/budget/{YYYY-MM}` does **not** change. It returns a bare JSON list today, and the
frontend parses a list. The budget page calls `GET /api/income/{YYYY-MM}` as well, and calculates
`leftToAssign` on the client, live, from the amounts in the form. The user then sees the number
move while they type (Spec 41).

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
with a budget role, explicit or default. The user sets `budgetRole` and `lastFour` on
`AccountsPage`, through the existing `PUT /api/accounts/{id}`. Add both fields to the editable
fields in `_put_account()` and in `AccountTable.update()`.

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

The LLM returns the keys that it returns today, `date`, `description`, `amount`, and `amount_invert`,
and one new optional key, `bankCategory`. A CSV with a separate debit column and credit column is
not supported today, and it stays out of scope.

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
   same absolute amount, a date at most 4 days away, and a different `accountId`. Look in the
   saved transactions of each upload month and of the month before and after it (any
   disposition). The same upload cannot hold the other side, because all its rows belong to one
   account. If exactly
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

Built in CP9 as `budget/csv_import/directions.py`. Two details: an account with no budget role (such as a
mortgage) counts as the outside world, and a row where no step finds the other side gets SKIP
and the reason `unknown_direction`.

The principle: when the import is not sure, it suggests SKIP and marks the row. A missed savings
transfer shows as a `$0` actual in a savings category, and the user sees it. A double count is hard
to see.

### Upload response

Today `POST /api/transactions/upload` takes `{csvData}` and returns three keys:
`{columnMapping, transactions, validations}`. `transactions[i]` is
`{transactionDate, description, amount, categoryId, categoryName}`. `validations[i]` is
`{index, issues}`, and `issues` holds `missing_category` and `locked_month` only. A CSV row that
the code cannot parse is dropped with no error.

The new request is `{csvData, accountId}`. `accountId` is required. The response keeps the same
three keys, and adds `accountId`. The new fields go on the existing objects:

```
{
  "accountId": "01J...",
  "columnMapping": { ... },
  "transactions": [
    {
      "transactionDate", "description", "amount", "categoryId", "categoryName",   // as today
      "disposition": "BUDGET" | "OFF_BUDGET" | "SKIP",
      "suggestedBy": "PAIR" | "LAST_FOUR" | "BANK" | "LLM" | "DEFAULT",
      "pairWith": { "yearMonth", "sortId" } | { "uploadIndex" } | null,
      "fingerprint": "…",
      "duplicate": bool
    }
  ],
  "validations": [
    {
      "index": 0,
      "issues": ["missing_category", "locked_month"],         // as today; these block the commit
      "review": ["no_pair_found", "llm_guess", "many_pairs"]  // new; these do not block
    }
  ]
}
```

`missing_category` now applies to a BUDGET row only, and to an OFF_BUDGET row with a `categoryId`
that does not exist. `_validate_commit_rows()` in the handler makes these checks for both the
upload and the commit. Change `ParsedTransaction`, `RowValidation`, and `RowValidationIssue` in
`types.ts`, and their parsers in `parsers.ts`, to match.

### Commit

Today the commit needs `transactionDate`, `description`, `amount`, and `categoryId` on each row.
The new rows also get `disposition`, `accountId`, `note`, and `fingerprint`, and `categoryId` is
required only for a BUDGET row. The client sends only the rows that the user selected. The server
does these steps:

1. Validate each row (the Transaction API rules above).
2. Run the editability check, as today.
3. Load the year-months of the batch, and drop each row whose fingerprint exists. The response
   lists them as `skippedDuplicates`.
4. Write the rest with `BatchWriteItem`, as today.

## Frontend

- **Routes and nav.** Add the `income` route in `App.tsx`, and an "Income" link in
  `BUDGET_LINKS` in `components/Layout.tsx`. Each new response shape needs a type in `types.ts`
  and a parser in `parsers.ts`, the same as the existing ones. `api.ts` gets one wrapper for each
  new route.
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
- **`AccountsPage`.** A budget role picker and a "last four digits" field. `_put_account()` in the
  handler and `AccountTable.update()` must accept `budgetRole` and `lastFour` as editable fields.
- **`AuditLogPage`.** It shows the new actions, with the label of the income source or the
  one-time line.

## Infrastructure

`PersonalFinanceDynamoDbStack`:

- Add `IncomeSource-{stage}` (PK `incomeSourceId`) and `IncomePlan-{stage}` (PK `yearMonth`, SK
  `lineId`). Pay per request, point-in-time recovery, and the same removal policy as the other
  tables.
- Add two `readonly` table properties, the same as the existing ones.
- Change the JSDoc of the stack class to list the two new tables (a rule in `CLAUDE.md`).

`PersonalFinanceWebStack` (it holds both the API handler and the scheduled densify Lambda,
`densifyFn`), and `infra/app.ts`, which passes each table to it:

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
- `test_personal_finance_handler.py` — the new routes, and the changes to upload and commit.
- `test_networth_models.py` — the `credit_card` account type, the `debt` class, and the rule
  that an asset account cannot choose `debt`.
- `test_budget_csv_import.py` — the local column types, the prompt without values, unique
  descriptions, the `kind` in the answer, fingerprints with `n`, the pair match (none, one, many),
  last four, the bank hint, and every row of the direction table.
- `infra/test/` — the two new tables, and the new environment variables and grants.

## Order of Work

`docs/budget-income-workplan.md` has the checkpoints, the files for each one, and the checks. In
short:

1. Dispositions and the note.
2. A generic densify step (a refactor, with no change in behavior).
3. The income plan: backend.
4. The income plan: frontend.
5. Left to assign. This step delivers the main goal (Spec 40).
6. Accounts.
7. to 9. The CSV import, in three parts.
10. The documentation.

## Open Items

- **An older problem, outside this work:** the upload stores the date text of the bank as it is
  (`row[date_idx].strip()`). The year-month is its first 7 characters. A bank format such as
  `03/15/2026` therefore gives a wrong year-month. `detect_column_types()` already knows several
  date formats, so a local date normalization is a small, separate change.
- **Fixed in CP5:** `compute_summary()` found the current month in UTC. It now uses ET, the same
  as `compute_income()`.
- **The column mapping without values** (Step 2) can be wrong about the sign. Test it with real
  CSVs from each bank before step 5 of the work.
- **The first upload after the change** can make copies of old rows, because old rows have no
  fingerprint (Step 4).
- **The definition of an income source.** The user wants to read it again (Specs 31 and 37).
