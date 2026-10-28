# Architecture - Personal Finance App

## What This Project Does

A finance tool for the family. It has two features today: a monthly household budget, and a net
worth record.

For the budget, a user signs in and sets a target for each category in a month. The user then
uploads a CSV export from a bank or a credit card, and the app fills in the transactions. The user
can then compare the actual amounts against the budget. The app records each budget change in an
audit log, and each change needs an explanation.

The budget also has an **income plan**. Each month holds its **planned incomes**: the recurring
incomes that carry forward, such as a salary or the rent of a property, and the one-time incomes,
such as a bonus. Each one has a gross amount and deductions, and the app calculates the net amount.
An **income source**, such as a job or a landlord business, is who pays it. The top line of the
budget is **left to assign**: the planned net income minus the budget targets. The goal is `$0`, so
every dollar of net amount has a job. See **Income Plan and Left to Assign**.

Each transaction has a **budget treatment**: Budget (a normal expense), Off-budget (such as work travel
that an employer pays back), or Skip (such as a paycheck or a transfer). Only Budget rows are
actuals. See **Budget Treatments**.

For a CSV import, the user chooses the account of the file. An LLM finds the columns from the
header names, and gives a kind and a category to each unique description. The app then suggests a
budget treatment from the direction of the money. See **LLM-Driven CSV Import**.

Each new feature goes in this same app, in a folder of its own.

A budget target and a transaction are permitted only in an **editable year-month**. That means the
current month, the previous month while its 7-day grace period continues, and the next 12 months.
A locked year-month is read-only by default. An **admin override** lets a privileged user change
one. The audit log records a budget-target change made through the override. It does not record a
transaction change — see **Audit scope**.

## Project Layout

```
web/personal-finance/    React + TypeScript frontend (Vite)
    src/
        pages/       Top-level route pages
        components/  Shared UI components
        api.ts       Typed API client
        auth.ts      Cognito PKCE auth + token refresh
        ...
lambda/src/corderohq/
    personal_finance_handler.py  Lambda entrypoint + HTTP routing
    scheduled_densify.py   Lambda that the monthly schedule calls (see Densification)
    budget/csv_import/     The CSV import; __init__.py exports every name
        llm.py             The two LLM calls: the column mapping and the categorization
        local_checks.py    Column types, dates, amounts and their sign, the bank category
        fingerprints.py    The fingerprint of each row, to find a row that was saved before
        directions.py      The direction table: a suggested budget treatment for each CSV row
    budget/treatment.py    The Budget-impacting / Off-budget / Skip rules of a transaction
    budget/transaction_args.py
                           The checks of a new or changed transaction, at the API boundary
    budget/income.py       The planned income types, their rules, and the checks of each request
    budget/densify.py      Densify step and future-month walk-back, for any DenseSparseStore
    budget/editability.py  The editability state function
    budget/auth.py         Admin group check
    accounts/              Account types, the type rules, and the checks of a create request;
                           the budget and the net worth record both use them
    networth/              Profile keys and the snapshot logic of the net worth record
    aws/dynamodb/          One file for each DynamoDB table; __init__.py exports every name
        util.py            The shared boto3 resource, the budget time zone, and the paging helpers:
                           scan_all_items() and query_all_items() return every page
        summary.py         compute_summary()
        income_summary.py  compute_income_plan()
        budget.py, budget_audit_log.py, category.py, category_group.py, transactions.py,
        income_source.py, planned_income.py, profile.py, account.py, net_worth_snapshot.py
                           One file for each table; the file name matches the table name
infra/lib/
    personal-finance-dynamodb-stack.ts   DynamoDB tables
    personal-finance-web-stack.ts        Cognito client, Lambda, API Gateway, CloudFront
```

## Infrastructure Overview

Two CDK stacks hold this app. They build on `DnsStack`, which holds the single `corderohq.com`
hosted zone, and on the shared `PlatformStack`, which holds the TLS certificate, the Cognito
user pool, and the `corderohq-alerts` SNS topic.

Both prod Lambda functions have a CloudWatch alarm on the `Errors` metric, and each alarm
publishes to that shared topic. The dev stage has no alarm, because a dev failure is normal
during development. See "Failure notifications" in `hsa-architecture.md` for the reason that
these alarms exist.

### PersonalFinanceDynamoDbStack

This stack owns the DynamoDB tables. It stays separate from the web stack, so that a deploy of the
app puts no data at risk. Every table bills for each request, and every table has point-in-time
recovery. The removal policy is `RETAIN` in prod, and `DESTROY` in dev.

### PersonalFinanceWebStack

The app layer. It makes a Cognito app client that uses PKCE and has no client secret. It also
makes a Lambda handler in Python 3.13, which can read and write DynamoDB and can call SSM
`GetParameter`. It then makes an API Gateway HTTP API with a Cognito JWT authorizer, and a
CloudFront distribution. That distribution serves both the React frontend and the API.

The CloudFront distribution has three behaviors:

- **Default** → the S3 origin, which serves the static React build. The cache is off in dev.
- **`/api/*`** → the API Gateway origin. It forwards every viewer header, so that the
  `Authorization` Bearer token reaches the Lambda. The cache is off.
- **`/oauth2/*`** → the Cognito hosted-UI domain origin. The cache is off.

`/oauth2/*` goes through CloudFront on purpose. Cognito then shares an origin with the app, so a
request for a new token can use the cookies and headers of that origin. No CORS is necessary.

### How They Connect

```
DnsStack (corderohq.com hosted zone)
    |
    +-- PlatformStack (shared TLS cert, shared Cognito User Pool)
            |
            +-- PersonalFinanceDynamoDbStack (DynamoDB tables)
            |       |
            +-------+-- PersonalFinanceWebStack (Cognito client, Lambda, API Gateway, CloudFront)
```

## Data Model (DynamoDB)

Each amount is an **integer, in millionths of a dollar**. For example, $12.34 becomes 12_340_000.
This keeps the app clear of floating-point error. The frontend converts each value before it shows
it.

```
+------------------------+--------------------+------------+-----------------------------+
| Table                  | PK                 | SK         | Notes                       |
+------------------------+--------------------+------------+-----------------------------+
| CategoryGroup-{stage}  | groupId            | —          | groupId is ULID. Fields:    |
|                        |                    |            | name, order, system (true   |
|                        |                    |            | only for the "Unassigned"   |
|                        |                    |            | group), timestamps. See     |
|                        |                    |            | Specs 23 to 30.             |
+------------------------+--------------------+------------+-----------------------------+
| Category-{stage}       | categoryId         | —          | categoryId is ULID. Fields: |
|                        |                    |            | name, active, groupId,      |
|                        |                    |            | orderInGroup, timestamps,   |
|                        |                    |            | nameHistory (list of        |
|                        |                    |            | {previousName, replacedAt   |
|                        |                    |            | ISO-8601 UTC}). Soft-delete |
|                        |                    |            | via active=False. Names are |
|                        |                    |            | unique case-insensitive     |
|                        |                    |            | across active + inactive —  |
|                        |                    |            | see **Category name         |
|                        |                    |            | uniqueness** below.         |
+------------------------+--------------------+------------+-----------------------------+
| Budget-{stage}         | yearMonth          | categoryId | yearMonth is YYYY-MM.       |
|                        |                    |            | carriedAmount and/or        |
|                        |                    |            | pinnedAmount (millionths).  |
|                        |                    |            | One row per category per    |
|                        |                    |            | month.                      |
+------------------------+--------------------+------------+-----------------------------+
| Transactions-{stage}   | yearMonth          | sortId     | sortId = {transactionDate}# |
|                        |                    |            | {ULID}. Fields:             |
|                        |                    |            | transactionDate,            |
|                        |                    |            | description, amount         |
|                        |                    |            | (signed millionths;         |
|                        |                    |            | positive = money out,       |
|                        |                    |            | negative = money in),       |
|                        |                    |            | budgetTreatment             |
|                        |                    |            | (BUDGET_IMPACTING /         |
|                        |                    |            | OFF_BUDGET / SKIP; absent   |
|                        |                    |            | means BUDGET_IMPACTING),    |
|                        |                    |            | categoryId (required for    |
|                        |                    |            | BUDGET_IMPACTING, absent    |
|                        |                    |            | for SKIP), accountId        |
|                        |                    |            | (optional), note (optional, |
|                        |                    |            | 200 chars), fingerprint     |
|                        |                    |            | (CSV rows only). Sort key   |
|                        |                    |            | sorts chronologically       |
|                        |                    |            | within a month.             |
+------------------------+--------------------+------------+-----------------------------+
| IncomeSource-{stage}   | incomeSourceId     | —          | ULID. Fields: owners (one or|
|                        |                    |            | more personIds), label,     |
|                        |                    |            | active, sortOrder,          |
|                        |                    |            | timestamps. It holds no     |
|                        |                    |            | money.                      |
+------------------------+--------------------+------------+-----------------------------+
| PlannedIncome-{stage}  | yearMonth          | planned-   | "REC#{ULID}": a recurring   |
|                        |                    | IncomeId   | income (kind RECURRING;     |
|                        |                    |            | carriedRecurringIncome      |
|                        |                    |            | and/or                      |
|                        |                    |            | pinnedRecurringIncome, each |
|                        |                    |            | {incomeSourceId, label,     |
|                        |                    |            | frequency, gross,           |
|                        |                    |            | deductions[{label, kind,    |
|                        |                    |            | amount}], endMonth?}), with |
|                        |                    |            | the same id in each month.  |
|                        |                    |            | Dense/sparse like Budget    |
|                        |                    |            | rows. "ONE#{ULID}": a one-  |
|                        |                    |            | time income (kind ONE_TIME; |
|                        |                    |            | label, incomeSourceId       |
|                        |                    |            | (optional), gross,          |
|                        |                    |            | deductions). Never carries  |
|                        |                    |            | forward.                    |
+------------------------+--------------------+------------+-----------------------------+
| BudgetAuditLog-{stage} | entityType         | sortId     | Single-partition design so  |
|                        | (always "AUDIT")   |            | recent-N queries are one    |
|                        |                    |            | Query with                  |
|                        |                    |            | ScanIndexForward=false. The |
|                        |                    |            | PK attribute is entityType, |
|                        |                    |            | and its value is always the |
|                        |                    |            | string "AUDIT". sortId is a |
|                        |                    |            | ULID (sortable by time).    |
|                        |                    |            | Fields: changedAt (ISO-8601 |
|                        |                    |            | UTC timestamp),             |
|                        |                    |            | effectiveYearMonth          |
|                        |                    |            | (nullable), categoryId (a   |
|                        |                    |            | budget entry) or            |
|                        |                    |            | plannedIncomeId and         |
|                        |                    |            | incomeSourceId (an income   |
|                        |                    |            | entry), action (CREATE,     |
|                        |                    |            | UPDATE, CLEAR, PIN, UNPIN,  |
|                        |                    |            | CATEGORY_HARD_DELETE, and   |
|                        |                    |            | the income actions),        |
|                        |                    |            | explanation, user ({sub,    |
|                        |                    |            | email, username}; see       |
|                        |                    |            | **Audit user identity**),   |
|                        |                    |            | override (bool), changes    |
|                        |                    |            | (shape varies by action).   |
|                        |                    |            | CREATE and UPDATE change a  |
|                        |                    |            | carried amount, CLEAR       |
|                        |                    |            | removes the own carried     |
|                        |                    |            | amount of a future month,   |
|                        |                    |            | and PIN and UNPIN set and   |
|                        |                    |            | remove a pinned amount.     |
|                        |                    |            | CATEGORY_HARD_DELETE comes  |
|                        |                    |            | from DELETE                 |
|                        |                    |            | /api/categories/{id}, with  |
|                        |                    |            | effectiveYearMonth null.    |
+------------------------+--------------------+------------+-----------------------------+
```

The net worth record has three more tables. This document does not describe the net worth record
in detail yet. See the code in `networth/` and in `aws/dynamodb/` (`profile.py`, `account.py`,
`net_worth_snapshot.py`).

```
+--------------------------+-----------------------+------------+----------------------------+
| Table                    | PK                    | SK         | Notes                      |
+--------------------------+-----------------------+------------+----------------------------+
| Profile-{stage}          | householdId (always   | personId   | One row for each person.   |
|                          | "HOUSEHOLD")          |            |                            |
| Account-{stage}          | accountId             | —          | accountType, liability,    |
|                          |                       |            | assetClasses, owners,      |
|                          |                       |            | active, sortOrder, and     |
|                          |                       |            | more. See accounts/.       |
|                          |                       |            | For the CSV import:        |
|                          |                       |            | cashFlowRole (SPENDING /   |
|                          |                       |            | HOLDING / EXTERNAL /       |
|                          |                       |            | NON_CASH; absent means the |
|                          |                       |            | default for the type) and  |
|                          |                       |            | accountNumberLastFour      |
|                          |                       |            | (list of 4-digit strings). |
|                          |                       |            | Every liability            |
|                          |                       |            | (credit_card, mortgage,    |
|                          |                       |            | other_liability) has the   |
|                          |                       |            | fixed asset class `debt`.  |
| NetWorthSnapshot-{stage} | yearMonth             | accountId  | One row for each account   |
|                          |                       |            | in each month.             |
+--------------------------+-----------------------+------------+----------------------------+
```

### Category name uniqueness

The Category-{stage} schema has no PK and no GSI on `name`. Therefore **the application code
enforces the unique name, and DynamoDB does not**. Case does not count:

1. On each `POST /api/categories` and each `PUT /api/categories/{id}` rename, the handler runs a
   `Scan` on the Category table. It makes each `name` lower-case, and compares it with the new
   name in lower-case. On a match it returns `409 Conflict`. If there is no match, it writes.

This method **has a race**. Two creates at the same moment, with the same name, both pass the check
on their own, and both write their rows. The table then holds two categories with one name. At the
scale of one household this does not matter, because one user in one browser tab cannot race
themselves. The code must carry a clear comment at the check, so that the next person does not
believe the check is complete.

**If this app must one day serve more than one household**, change it this way:

- Use a sentinel item in the same table. Write the Category row and a second item together, in one
  `TransactWriteItems` call. Give the second item `PK = "NAME#{lowercased}"`, and an SK such as `_`
  to fill the space, because the table has no SK today. Give each write the condition
  `attribute_not_exists(PK)`. The whole transaction then fails if another writer holds the name
  already. This design has no race, and it is the correct one. It is not worth the work today: the
  schema gains a PK of two kinds, and the rename path must write the sentinel again.

## Request Flow

```
Browser --> CloudFront --> S3 (React app)
                |
                +--> /api/*    --> API Gateway --> Lambda (budget_handler.handler)
                |     (JWT auth via Cognito User Pool authorizer)
                |
                +--> /oauth2/* --> Cognito hosted UI (same-origin)
```

The frontend signs the user in with the Cognito hosted UI. It uses the authorization-code flow with
PKCE. After the callback, the app keeps the tokens. It then sends the access token on each API call,
as `Authorization: Bearer ...`. On a 401 it gets a new token, and it permits only one such request
at a time.

## API Routes (Lambda)

Every route needs a JWT, and every route sits under `/api`. The handler reads
`(method, rawPath)`, and sends each request to a private function.

```
+--------+------------------------------------------+------------------------------------+
| Method | Path                                     | Purpose                            |
+--------+------------------------------------------+------------------------------------+
| GET    | /api/categories?include_inactive=true    | List categories (active only by    |
|        |                                          | default).                          |
+--------+------------------------------------------+------------------------------------+
| GET    | /api/category-groups                     | List the category groups.          |
| POST   | /api/category-groups                     | Create a group. Body: name.        |
| PUT    | /api/category-groups/{id}                | Rename a group.                    |
| DELETE | /api/category-groups/{id}                | Delete a group (Spec 28).          |
| POST   | /api/category-groups/reorder             | Write the full group order.        |
| POST   | /api/categories/reorder                  | Write the category order in one    |
|        |                                          | group.                             |
+--------+------------------------------------------+------------------------------------+
| GET    | /api/categories/{id}?deletion_preview=   | Admin-only. Returns the hard-      |
|        | true                                     | delete impact summary (counts of   |
|        |                                          | Budget rows, Transactions, locked  |
|        |                                          | months affected, audit entries     |
|        |                                          | that will become unresolvable).    |
|        |                                          | See **Hard delete semantics**.     |
+--------+------------------------------------------+------------------------------------+
| POST   | /api/categories                          | Create category. Body requires     |
|        |                                          | name and groupId. initialTarget is |
|        |                                          | optional: a non-negative integer   |
|        |                                          | in millionths, default 0 (Spec 14).|
|        |                                          | If the current year-month is       |
|        |                                          | dense, creates a row at            |
|        |                                          | initialTarget in it.               |
+--------+------------------------------------------+------------------------------------+
| PUT    | /api/categories/{id}                     | Rename category. Appends a         |
|        |                                          | nameHistory entry capturing the    |
|        |                                          | previous name and replacedAt time. |
+--------+------------------------------------------+------------------------------------+
| POST   | /api/categories/{id}/deactivate          | Soft-delete. Returns {category,    |
|        |                                          | affectedMonths} where              |
|        |                                          | affectedMonths lists the future    |
|        |                                          | months with their own values for   |
|        |                                          | this category. Body {confirm:      |
|        |                                          | bool}. If confirm=true, the        |
|        |                                          | deactivation deletes those future  |
|        |                                          | rows, and writes one CLEAR audit   |
|        |                                          | entry for each carried amount and  |
|        |                                          | one UNPIN entry for each pinned    |
|        |                                          | amount. If confirm is absent or    |
|        |                                          | false and affectedMonths is non-   |
|        |                                          | empty, it returns 409 so the UI    |
|        |                                          | can prompt.                        |
+--------+------------------------------------------+------------------------------------+
| POST   | /api/categories/{id}/reactivate          | Reactivate a deactivated category. |
|        |                                          | The future values dropped at       |
|        |                                          | deactivation are NOT restored.     |
+--------+------------------------------------------+------------------------------------+
| DELETE | /api/categories/{id}                     | Hard delete. Admin-only            |
|        |                                          | (budget-admin-{stage} group claim  |
|        |                                          | required). Cascade-deletes the     |
|        |                                          | Category row, every Budget row     |
|        |                                          | referencing it (all year-months,   |
|        |                                          | including locked), and every       |
|        |                                          | Transaction referencing it (all    |
|        |                                          | year-months, including locked).    |
|        |                                          | Writes a CATEGORY_HARD_DELETE      |
|        |                                          | audit entry. Body requires         |
|        |                                          | confirm=true, confirmName matching |
|        |                                          | the current category name, and     |
|        |                                          | explanation. Hidden from the       |
|        |                                          | normal categories page. See        |
|        |                                          | **Hard delete semantics**.         |
+--------+------------------------------------------+------------------------------------+
| GET    | /api/budget/{YYYY-MM}                    | Get targets for a month. For dense |
|        |                                          | months (locked/grace/current),     |
|        |                                          | returns the stored rows directly.  |
|        |                                          | For future months, returns         |
|        |                                          | resolved effective values per      |
|        |                                          | category with an `inherited: bool` |
|        |                                          | flag: true when the carried amount |
|        |                                          | comes from the walk back, false    |
|        |                                          | when the month holds its own. Lazy |
|        |                                          | hydration runs here if the current |
|        |                                          | month is not yet dense — see       |
|        |                                          | **Carry-Forward**.                 |
+--------+------------------------------------------+------------------------------------+
| POST   | /api/budget/{YYYY-MM}/replace            | Dense-month write (current, grace, |
|        |                                          | or — via override — locked).       |
|        |                                          | Replace-all semantics; body must   |
|        |                                          | include every category with an     |
|        |                                          | existing row or a non-zero         |
|        |                                          | transaction in this month. Writes  |
|        |                                          | per-category audit entries for     |
|        |                                          | every diff.                        |
+--------+------------------------------------------+------------------------------------+
| POST   | /api/budget/{YYYY-MM}/update             | Future-month write. Body lists     |
|        |                                          | only the categories the user       |
|        |                                          | changes. Upsert semantics;         |
|        |                                          | categories omitted from the body   |
|        |                                          | are untouched. `carriedAmount:     |
|        |                                          | null` removes the month's own      |
|        |                                          | carried amount. Returns            |
|        |                                          | laterCarriedChanges and            |
|        |                                          | matchesInheritedValue warnings per |
|        |                                          | category — see **Update write      |
|        |                                          | semantics**. Writes per-category   |
|        |                                          | audit entries: UPDATE or CLEAR for |
|        |                                          | a carried amount, PIN or UNPIN for |
|        |                                          | a pinned amount.                   |
+--------+------------------------------------------+------------------------------------+
| GET    | /api/transactions?month=YYYY-MM          | List transactions for a month.     |
+--------+------------------------------------------+------------------------------------+
| POST   | /api/transactions                        | Create one transaction.            |
+--------+------------------------------------------+------------------------------------+
| POST   | /api/transactions/upload                 | Body {csvData, accountId,          |
|        |                                          | flipSigns?}. Returns the parsed    |
|        |                                          | rows with a suggested budget       |
|        |                                          | treatment and category,            |
|        |                                          | fingerprints,                      |
|        |                                          | duplicate flags, and per-row       |
|        |                                          | issues and review reasons. Nothing |
|        |                                          | is committed. See **LLM-Driven CSV |
|        |                                          | Import**.                          |
+--------+------------------------------------------+------------------------------------+
| POST   | /api/transactions/commit                 | Body {rows, override?}. Checks     |
|        |                                          | every row first; rejects the batch |
|        |                                          | if any row has an issue. Drops     |
|        |                                          | each row whose fingerprint exists  |
|        |                                          | (skippedDuplicates).               |
+--------+------------------------------------------+------------------------------------+
| POST   | /api/transactions/update                 | Update mutable fields (description,|
|        |                                          | amount, budgetTreatment,           |
|        |                                          | categoryId, accountId, note).      |
+--------+------------------------------------------+------------------------------------+
| POST   | /api/transactions/delete                 | Body {items: [{yearMonth, sortId}, |
|        |                                          | ...]}. Batch delete.               |
+--------+------------------------------------------+------------------------------------+
| GET    | /api/summary?month=YYYY-MM               | Per-category budgeted vs. actual + |
|        |                                          | totals, offBudget, totalsWith-     |
|        |                                          | OffBudget, and income {planned-    |
|        |                                          | NetIncome, leftToAssign}.          |
+--------+------------------------------------------+------------------------------------+
| GET    | /api/audit-log?limit=N                   | Recent audit entries. Default N=10,|
|        |                                          | server cap N=200.                  |
+--------+------------------------------------------+------------------------------------+
```

The income plan has its own routes, which copy the budget routes. See **Income Plan and Left to
Assign** for the bodies and the responses.

```
+--------+--------------------------------------------+----------------------------------------+
| Method | Path                                       | Purpose                                |
+--------+--------------------------------------------+----------------------------------------+
| GET    | /api/income-sources?include_inactive=true  | List the income sources.               |
| POST   | /api/income-sources                        | Create a source. It holds no money, so |
|        |                                            | it has no audit entry.                 |
| POST   | /api/income-sources/reorder                | Write the full order.                  |
| PUT    | /api/income-sources/{id}                   | Change the label or the owners. No     |
|        |                                            | editability check. No audit entry.     |
| POST   | /api/income-sources/{id}/deactivate        | End each of its recurring incomes in   |
|        |                                            | the current month.                     |
| POST   | /api/income-sources/{id}/reactivate        | The same as a category.                |
| GET    | /api/income/{YYYY-MM}                      | The income plan of one month.          |
| POST   | /api/income/{YYYY-MM}/replace              | Dense-month write of the recurring     |
|        |                                            | incomes.                               |
| POST   | /api/income/{YYYY-MM}/update               | Future-month write of the recurring    |
|        |                                            | incomes.                               |
| POST   | /api/income/{YYYY-MM}/one-time             | Create a one-time income (no           |
|        |                                            | `plannedIncomeId`), change it (an id), |
|        |                                            | or delete it (an id and `delete:       |
|        |                                            | true`).                                |
+--------+--------------------------------------------+----------------------------------------+
```

API Gateway lists each route explicitly. A route that is not in that list gets a `404` from API
Gateway, and the Lambda never sees the request.

The net worth record has its own routes: `/api/profiles/...`,
`/api/accounts/...`, and `/api/net-worth/...`. See `handler()` in `personal_finance_handler.py`.

A change to a transaction uses `POST`, and not `PUT` or `DELETE`. A CloudFront cache policy can
remove the body from the other two methods.

Every response carries strict security headers: `HSTS`, `X-Content-Type-Options`,
`X-Frame-Options`, and `Cache-Control: no-store`.

These endpoints have an **editability gate**: `POST /api/budget/{YYYY-MM}/replace`,
`POST /api/budget/{YYYY-MM}/update`, `POST /api/transactions`, `POST /api/transactions/update`,
`POST /api/transactions/delete`, `POST /api/transactions/commit`, and the income plan writes
(`/api/income/{YYYY-MM}/replace`, `/update`, and `/one-time`). The income `/update` copies the
budget `/update`: the override does not unlock a month that is not in the future. In the income
`/update`, the override only lifts the rules of the end month. Each one accepts an optional
`override: true` flag in the body. The handler obeys that flag only if the JWT shows that the user
is in the admin group — see **Editability and the Admin Override**. If it does not, the handler
returns `403`. The flag does nothing on a `GET` endpoint, because any signed-in user may read
without limit. The other endpoints that change data — `POST /api/categories`, the
`PUT /api/categories/{id}` rename, `/deactivate`, and `/reactivate` — have no editability gate,
and they do not accept `override`. `DELETE /api/categories/{id}` is admin-only through a different
route, with no `override` flag. See **Hard delete semantics**.

A write to a budget target makes an audit log entry. This applies to `/replace`, to `/update`, and
to `/deactivate`. `/deactivate` makes an entry only when it drops a future value, and it then writes
one `CLEAR` or `UNPIN` entry for each amount it drops. `DELETE /api/categories/{id}` also writes one
`CATEGORY_HARD_DELETE` entry. The app does not audit a change to a transaction, whether an insert,
an update, a delete, or a commit. It also does not audit `POST /api/categories`, the
`PUT /api/categories/{id}` rename, or `/reactivate`. See **Audit Scope** below.

The `PUT /api/categories/{id}` rename has no editability check. A rename is always permitted. It
adds to the `nameHistory` list on the Category row, so that a locked month can show the name that
was in use at that time.

## Editability and the Admin Override

The server computes the range of year-months that a user may change. It does this on each request
that changes data. A client never decides what is editable. The Lambda is the only authority.

### Constants

```
N_FUTURE_MONTHS = 12
GRACE_PERIOD_DAYS = 7
BUDGET_TZ = "America/New_York"
```

### State function

`editability(yearMonth, now_utc) -> {EDITABLE, GRACE, LOCKED}` where:

1. Convert `now_utc` to `BUDGET_TZ` to obtain `now_et`.
2. `current_ym = (now_et.year, now_et.month)`.
3. If `yearMonth == current_ym`: `EDITABLE`.
4. If `yearMonth` is one of the next `N_FUTURE_MONTHS` after `current_ym`: `EDITABLE`.
5. If `yearMonth` is `current_ym - 1 month` AND `now_et < (first of current_ym + GRACE_PERIOD_DAYS, 00:00 ET)`: `GRACE`.
6. Otherwise: `LOCKED`.

For a change check, `GRACE` and `EDITABLE` mean the same thing. The two names exist so that the
frontend can show a banner that says "grace ends in X days".

`EDITABLE` covers the current year-month and the next 12. The `/update` endpoint adds a stricter rule
on top: the month must be `EDITABLE` **and** later than `current_ym`. So `/update` rejects the current
month and the grace month, although both are editable, and the user goes to `/replace` instead.

The month changes at **midnight ET on day 1** of the new month, whatever the daylight saving
offset. The grace period ends at **midnight ET on day 8** of the new month.

### Enforcement

| Endpoint | Editability check |
|----------|------------------|
| `POST /api/budget/{YYYY-MM}/replace` | `{YYYY-MM}` must be the current month, GRACE, or — via override — LOCKED |
| `POST /api/budget/{YYYY-MM}/update` | `{YYYY-MM}` must be strictly in the future — i.e., EDITABLE and `yearMonth > current_ym` (one of the next 12 months). A write to the current month, the grace month, or any locked month is rejected with `409`; those months are dense and `/replace` is the right endpoint. Override does not unlock `/update` for non-future months — it has no use here |
| `POST /api/transactions` | `transactionDate`'s year-month must be EDITABLE or GRACE |
| `POST /api/transactions/update` | The `yearMonth` of the row must be EDITABLE or GRACE. An update cannot change `transactionDate` |
| `POST /api/transactions/delete` | Each item's `yearMonth` must be EDITABLE or GRACE |
| `POST /api/transactions/commit` | Every row's `transactionDate` year-month must be EDITABLE or GRACE; see **CSV commit semantics** |
| `POST /api/income/{YYYY-MM}/replace` | The same as the budget `/replace` |
| `POST /api/income/{YYYY-MM}/update` | The same as the budget `/update` |
| `POST /api/income/{YYYY-MM}/one-time` | `{YYYY-MM}` must be EDITABLE (a future month too) or GRACE, or LOCKED via override |

On a failure the response is `409 Conflict`. The body lists every year-month and every row at
fault. A change to one item reports that one item. A change to a batch reports every item that
failed, in one response. The app never commits part of a batch, and it never reports one error at
a time.

### Admin override

The override is a Cognito user pool group, named `budget-admin-{stage}` — for example
`budget-admin-dev` and `budget-admin-prod`. The name holds the stage on purpose, so that an admin
in dev is not an admin in prod. The Lambda reads its `STAGE` environment variable, and it builds
the group name it expects on each request.

The API Gateway JWT authorizer puts the `cognito:groups` claim from the access token into the
Lambda event. The handler then does these steps:

1. It reads `override: true` from the request body. The default is `false`.
2. If `override` is true, it looks for `budget-admin-{STAGE}` in `cognito:groups`. If that group is
   absent, it returns `403`.
3. If the group is present, it skips the editability check, for this one request only.
4. For a write to a budget target, through `/replace` or `/update`, it sets `override: true` on every
   audit entry that the request makes.

The hard delete, `DELETE /api/categories/{id}`, uses the same group claim. It does not use the
`override` flag, because only an admin may do it in any case. See **Hard delete semantics**.

**Admin Operating Model**, below, covers how an admin works day to day: how to make the first
admin, the toggle in the frontend, what each action does, and the admin tools page.

### Audit scope

Recorded in `BudgetAuditLog-{stage}`:

- `POST /api/budget/{YYYY-MM}/replace` → one `CREATE` or `UPDATE` entry for each changed carried
  amount, and one `PIN` or `UNPIN` entry for each changed pinned amount. `/replace` cannot remove a
  row by any correct use, because the body must hold every row that exists. It therefore never
  makes a `DELETE` entry. Three other paths delete a budget row, and each writes a different entry:
  `/update` with `carriedAmount: null` writes one `CLEAR` entry; a deactivation that drops a future
  row writes one `CLEAR` or `UNPIN` entry for each amount it drops; and a hard delete removes every
  row for the category and writes one `CATEGORY_HARD_DELETE` entry.
- `POST /api/budget/{YYYY-MM}/update` → one `UPDATE` or `CLEAR` entry for each changed carried
  amount, and one `PIN` or `UNPIN` entry for each changed pinned amount.
- `POST /api/categories/{id}/deactivate`, when `confirm` is `true` and `affectedMonths` holds at
  least one month → one `CLEAR` or `UNPIN` entry for each amount it drops. The budget-target audit
  log therefore stays complete, even when the app drops a value as a side effect. The deactivation
  itself makes no entry.
- `DELETE /api/categories/{id}` → one `CATEGORY_HARD_DELETE` entry. This breaks the rule that the
  app does not audit the life cycle of a category. It breaks it because no one can undo a hard
  delete, and the delete destroys historical data. The entry is then the only trace left of the
  category and of the action.

### Audit entry `changes` payload

The shape of the `changes` field on a `BudgetAuditLog-{stage}` row depends on the action:

- `CREATE` — `/replace` wrote a new Budget row.
  `{carriedAmount: {before: null, after: number}}`.
- `UPDATE` — `/replace` changed a carried amount, or `/update` set the own carried amount of a future
  month. `{carriedAmount: {before: number | null, after: number}}`. `before` is `null` if the future
  month had no own value.
- `CLEAR` — the own carried amount of a future month is gone. Either `/update` with
  `carriedAmount: null` removed it, or a deactivation dropped it.
  `{carriedAmount: {before: number, after: null}}`.
- `PIN` and `UNPIN` — a pinned amount is set or removed. `{pinnedAmount: {before, after}}`.
- `CATEGORY_HARD_DELETE` — `{budgetRowsDeleted: number, transactionsDeleted: number, name: string}`.
  This holds counts and a snapshot, and not a before-and-after pair, because the category and its
  data are gone.

### Audit entries of the income plan

The income plan writes these actions to the same log. An income entry has no `categoryId`. It has
`plannedIncomeId`, and also `incomeSourceId` when the income has a source.

| Action | Made by | `changes` |
|---|---|---|
| `RECURRING_INCOME_CREATE` | a `SET` with no `plannedIncomeId` | `{recurringIncome: {before: null, after: R}}` |
| `RECURRING_INCOME_UPDATE` | a `SET` with a `plannedIncomeId`; the deactivation of a source, which sets `endMonth` | `{recurringIncome: {before: R \| null, after: R}}` |
| `RECURRING_INCOME_CLEAR` | a `CLEAR` in `/update`; a deactivation that drops a future value | `{recurringIncome: {before: R, after: null}}` |
| `RECURRING_INCOME_PIN` / `_UNPIN` | a `SET_PIN` or a `CLEAR_PIN`; a deactivation that drops a future pinned value | `{pinnedRecurringIncome: {before: R \| null, after: R \| null}}` |
| `ONE_TIME_INCOME_CREATE` / `_UPDATE` / `_DELETE` | the `/one-time` route | `{oneTimeIncome: {before: O \| null, after: O \| null}}` |

`R` is a recurring income, `{incomeSourceId, label, frequency, gross, deductions, endMonth?}`, and
`null` is no value. `O` is a one-time income, `{label, incomeSourceId?, gross, deductions}`. The
entry holds the whole value, because a one-time income can be deleted later. The create, the
rename, the reorder, and the reactivation of an income source make no entry, the same as for a
category.

### Audit entry `override` field

The `override` boolean shows that the entry came from an action that needed admin rights. It is
`true` in two cases. First, the request carried `"override": true` and the JWT carried the admin
group claim. Second, only an admin can do the action, and the action has no override flag.
`CATEGORY_HARD_DELETE` is the only action of the second kind. It is `false` for every normal change
by a user who is not an admin. The Audit Log page shows this flag clearly, so that a reviewer can
select everything that needed an admin.

### Audit user identity

Each entry stores the actor as a nested object:

```
user: {
  sub: "<cognito-user-sub-uuid>",   // immutable; the canonical identifier
  email: "user@example.com",         // at-time-of-action snapshot
  username: "user@example.com"       // at-time-of-action snapshot (Cognito username, often = email)
}
```

The app reads all three fields from the JWT claims at the time of the request — `sub`, `email`, and
`cognito:username` — and writes them into the entry exactly as they are. This snapshot matters. If
the user later changes their email or their Cognito username, the audit log still shows the value
that was in use at the time of the action. That fits the purpose of the log, which is a historical
record. `sub` is the only field that always points to the same person. The other two may change on
the live user record, and that is intended.

The Audit Log page shows `email` as the main label for each entry. A hover shows the `sub` and the
`username`, to tell two similar entries apart.

The app does not record these:

- A change to a transaction — an insert, an update, a delete, or a commit — even through the
  override. This is an accepted trade-off. An override change to a transaction in a locked month
  leaves no direct trace. It is acceptable for two reasons. First, the history of the budget
  targets is what matters most. Second, the summary for a locked month still shows the new state of
  the transactions, so a person can find the change by a look at the historical view.
- The creation, the rename, and the reactivation of a category. A person can undo each of these, or
  work each one out from the current Category row, so an entry would add little.
- The deactivation itself. There is no "the app deactivated this category" entry. A deactivation
  does write one `CLEAR` or `UNPIN` entry for each amount it drops, as above. Those entries cover the changes to
  the *budget targets* that follow from it. The deactivation itself stays unrecorded.

### Historical fidelity

A year-month locks at the end of its grace period. After that, no one can change its rows in the
Budget table or in the Transactions table, except through the admin override. The app audits the
override path. So the summary endpoint for a locked month returns the same values in 20 years as
it did on the day the month locked. The audit log explains any difference that an override made.

The summary endpoint chooses its categories as **Spec 5** states:

- **An editable year-month** — the categories that are active now, and any category, active or
  not, with at least one transaction in that month whose `amount` is not 0. Because an amount
  carries a sign, this covers a category with income or returns only, where the amounts add up to
  a negative number. It covers any category with money movement of any kind.
- **A locked year-month** — every category with a budget row in that month, whatever the amount,
  and every category with a transaction in that month. Here `$0` counts as a target, and no row
  means no target. The current `active` flag does not matter. The app never deletes a past budget
  row when a category is deactivated, so a row in the Budget table for that month is the
  historical signal. No separate snapshot is necessary.

### Display names on locked months

For a summary of a locked month, the app finds the name of each category in `Category.nameHistory`:

1. Compute `month_end_utc`. This is the instant 23:59:59.999999 on the last day of the year-month
   in `BUDGET_TZ`, converted to UTC. For `2024-03` it is `2024-03-31T23:59:59.999999-04:00` in EDT,
   which is `2024-04-01T03:59:59.999999Z`.
2. Find the first entry in `nameHistory` whose `replacedAt`, an ISO-8601 UTC time, is **later than**
   `month_end_utc`. It must be later, and not equal.
3. If such an entry exists, its `previousName` was the name in use during that month. Return it as
   `historicalName`, next to the current `name`. The frontend then shows the pair, such as
   `Groceries (renamed to: Food)`.
4. If no such entry exists, the current `name` was already in use during that month. Return only
   `name`, and set `historicalName` to `null`.

Step 1 must use the daylight saving offset of the last day of that year-month, and not a fixed
offset. This matters at a month boundary near a daylight saving change.

A response for an editable month always returns the current `name` only. No rename can come after
an editable month while that month is still editable.

### Replace write semantics

`POST /api/budget/{YYYY-MM}/replace` writes a dense year-month. That is the current month, the
previous month while it is in grace, or any locked month through the admin override. The body:

```
{
  "targets": [{ "categoryId", "carriedAmount": number, "pinnedAmount"?: number | null }, ...],
  "explanation": "...",
  "override": false
}
```

This endpoint writes one row for each target, and each write replaces the row of its category. It
deletes no row, so a failure part way never leaves the month without its targets. The handler
checks the request in this order:

1. **Editability**, as the **Enforcement** table states. If the app cannot write `{YYYY-MM}`, and no
   override applies, the handler returns `409` with the editability error and stops. It does not
   check the body.
2. **A complete body:**
   - The body must hold every category with a budget row in that month, even if its amount does
     not change.
   - The body must hold every category with a transaction in that month whose amount is not zero.

If the body fails either rule, and editability passed, the response is `409 Conflict`. The body
lists each `categoryId` that is absent, and the app **writes no rows**. The client must send the
request again with those entries added. Usually it copies the values that exist.

`carriedAmount` must be a number. `0` is valid, and it is a real target. A carried amount is never
`null`. A `pinnedAmount` key sets the pinned amount (a number) or clears it (`null`). Without the
key, the stored pinned amount stays.

Each difference makes one `CREATE` or `UPDATE` entry in the audit log, for one category.
`/replace` never makes a `DELETE` entry. The body rule stops a client from removing a row by
leaving it out.

### Update write semantics

`POST /api/budget/{YYYY-MM}/update` writes the own values of one or more categories in a future
editable year-month. Body:

```
{
  "targets": [{ "categoryId", "carriedAmount"?: number | null, "pinnedAmount"?: number | null }, ...],
  "explanation": "..."
}
```

This endpoint writes a row, or changes the row that is there. It does not touch a category that the
body leaves out, or a key that a target leaves out. Each target needs at least one of the two keys.

`carriedAmount: number` → the month holds that carried amount as its own. `carriedAmount: null` →
the app removes it, and the month inherits again. `pinnedAmount` works the same way for the pinned
amount. A row with neither amount left is **deleted**. No carried amount means that the display
walks back to find a value.

The response holds the month and the warnings:

```
{
  "targets": [{ "yearMonth", "categoryId", "carriedAmount", "inherited", "pinnedAmount"? }, ...],
  "warnings": {
    "laterCarriedChanges": [{ "categoryId", "yearMonth" }, ...],
    "matchesInheritedValue": ["categoryId", ...]
  }
}
```

- **laterCarriedChanges** — for each category in the request, this lists the later months, inside
  the editable range, that hold their own carried amount. The change stops at them. The frontend
  shows them, so that the user can clear them in a second call. (Spec 19.)
- **matchesInheritedValue** — this appears for each category where the new carried amount equals
  the value that the month inherits already. The frontend then warns the user: the month now holds
  that value as its own, so a later change in an earlier month stops there. (Spec 20.)

## Carry-Forward

### Storage model: dense vs. sparse

The Budget table holds two kinds of row only: a row the user wrote, and a row the app made dense.

- **A locked month, a grace month, and the current month are always *dense*.** Each holds exactly
  one row for each category that existed when the app made the month dense, and any row the user
  wrote after that. For density, only the row itself counts.
- **A future month is *sparse*.** It holds only the rows the user wrote, through
  `POST /api/budget/{YYYY-MM}/update`. A category with no own value there has no row at all.

This split has three results, and each is worth a clear statement:

1. **A locked month never walks back.** The dense set of rows is the historical record. The app
   writes that set when it makes the month dense, either by the cron job at the month change, or
   later when it catches up. A change to an earlier locked month, through the admin override,
   cannot change what a later locked month shows.
2. **A change to a future month touches one row.** To set `December 2026 ABC = $2000` writes one
   row. `DEF` gets no row in December until the user sets it, or until December arrives and the app
   makes the month dense.
3. **The app resolves a future month one category at a time.** For each category that is active
   now: if a carried amount exists at `(future_ym, category)`, use it. If not, walk back one month at a time,
   with no fixed limit, until a row appears or the history ends. Then use the carried amount on the
   row it found. If it finds no row at all, the category has no target.

### Densification

To densify is to change the current year-month from sparse to dense. This happens in two ways:

**On a schedule.** EventBridge Scheduler starts a Lambda at `0 0 1 * ? *`, with
`ScheduleExpressionTimezone: "America/New_York"`. That is midnight ET on day 1 of each month. The
job is idempotent, so a second run does nothing.

**On demand, as a fallback.** The scheduled job may not have run for a year-month. It may have
failed, or the user may return after a long time away. The app then densifies during the next
request about the budget. Four requests start it: any `GET /api/budget/{yearMonth}`, whichever
month the caller asks for; any `GET /api/summary?month={yearMonth}`; and any budget write, through
`/replace`, `/update`, or `POST /api/categories`. The handler always brings the table up to
`current_ym` before it answers. This holds even when the caller asks for a past or a future month.
The current month must be dense first, or the walk-back and the walk-forward give wrong results.
When this runs during a request, the request waits for it to finish.

### Walk-forward through missed months

The app first finds the year-months after `M_last`, up to and including `current_ym`. `M_last` is the
most recent month before `current_ym` that has a row. `current_ym` is the month that holds now, in ET.
The range always includes `current_ym`, even when it has rows already. A value that the user set in
advance, through `/update`, puts a row in `current_ym` before the month starts. That row does not make
the month dense. The app then takes each month `M` in that range, oldest first:

1. For each category that is active now:
   - If a row exists at `(M, categoryId)`, from a value the user wrote earlier, leave it.
   - If not, walk back from `M - 1` to `M_last`, and find the most recent row for that
     `categoryId`. Copy its `carriedAmount`.
   - If no earlier row exists anywhere, write a row with `carriedAmount=0`.
2. The app adds no inactive category. It leaves in place an own value on an inactive category that exists
   already at `M`. The user may make the category active again, or may want the history.

**The cold start, when no dense month exists.** `M_last` has no value when the Budget table has no
row before `current_ym`, so there is nothing to walk back to. The app then densifies `current_ym` only:
it writes a row with `carriedAmount=0` for each category that is active and has no row there. This is almost impossible in normal use. It
needs a new deploy, with categories made already, and before anyone opens a budget page. If it does
happen, `$0` is a safe start, and the user can change it in the usual way.

The app moves forward one month at a time. It does not collapse the work into one walk-back from
`current_ym`. Each month between therefore becomes dense on its own, and the history stays true if
someone looks at one of those months later, as a locked month.

**A note for the implementation: get the whole range in one call.** Both the densify step and the
display of a future month walk back through Budget rows, one category and one month at a time. The
simple way to write this makes `O(categories × months)` calls. Instead, the handler must first load
the whole `[M_last, current_ym + N_FUTURE_MONTHS]` range in one `BatchGetItem`, or in a few
`Query` calls on the `yearMonth` partitions. It puts the result in a Python
`dict[(yearMonth, categoryId), carriedAmount]`, and then does every walk-back in memory. This applies to
both of these:

- The densify step at the month change, which walks back from each `M` to find an earlier row.
- `GET /api/budget/{future_ym}` and `GET /api/summary?month={future_ym}`, which walk back for each
  active category to find the target.

### Category lifecycle hooks

- **Create** (`POST /api/categories`) — the body must hold an `initialTarget` that is not zero and
  not negative. If `current_ym` is not dense, the handler densifies it first. It then writes a row
  at `(current_ym, newCategoryId, initialTarget)`, so that the new category appears at once in a
  summary of the kind Spec 1 describes. That row then takes part in a later walk-back for a future
  month. A new category with `initialTarget=$50` therefore shows `$50` in every future editable
  month, until the user sets a different amount.
- **Deactivate** (`POST /api/categories/{id}/deactivate`) — this returns
  `affectedMonths: [yearMonth, ...]`. It lists every **future** month with its own value for this
  category. With `confirm: true`, the transaction deletes those future rows, and it writes one
  `CLEAR` or `UNPIN` audit entry for each amount it drops. The budget-target audit trail therefore stays
  complete — see **Audit scope**. The app leaves the past dense rows as they are, in the current
  month, the grace month, and every locked month. The history therefore stays true, as Spec 2
  requires.
- **Reactivate** (`POST /api/categories/{id}/reactivate`) — this changes the flag back. It does not
  restore a future value that the deactivation dropped. The category joins the active set again. The
  next densify step, or any write to the current month, then treats it like a new category for
  that month.

### Pinned values (Spec 58)

A Budget row and a recurring income can each hold **two values**:

| | Budget row | Recurring income (PlannedIncome) | Later months |
|---|---|---|---|
| Carried value | `carriedAmount` | the recurring income itself | copy it |
| Pinned value | `pinnedAmount` | `pinnedRecurringIncome` (a whole recurring income) | never see it |

- **What a month shows:** its pinned value if it has one, else its carried value, else
  the carried value from the walk back (`effective_value()` in `budget/densify.py`).
- **The densify step and the walk back read carried values only.** The densify adapters
  (`BudgetDensifyAdapter`, `RecurringIncomeDensifyAdapter`) give them through `scan_stored_values()`, and
  give the pinned values through `scan_pinned_values()`. When the densify step fills the
  carried value of a month, `put_stored_value()` keeps a pinned value that the row already
  has.
- **A future month can hold a row with only a pinned value,** and no carried value. The month then
  inherits its carried value, and `laterCarriedChanges` leaves the row out.
- **The writes of a budget target:**
  - `/replace` (a dense month): each target sends its carried value, as before. A `pinnedAmount` key
    sets the pinned value (a value) or clears it (`null`). Without the key, the stored one
    stays.
  - `/update` (a future month): each of the two keys is optional. A value sets it, `null` removes it,
    and a missing key does not change it. A row with neither value is deleted.
- **The writes of a recurring income:** two more actions, in `/replace` and in `/update`, each with
  a `plannedIncomeId`. `SET_PIN` sets the pinned value, and `CLEAR_PIN` removes it. `SET` and `CLEAR`
  change only the carried value, and keep the pinned value. An entry with neither value is deleted.
- **The frontend** sends "From this month on" as the carried value plus a cleared pinned
  value, and "This month only" as the pinned value alone.
- **The audit log** writes `PIN` and `UNPIN` (and the `RECURRING_INCOME_` forms) for a pinned change. The `changes` payload names `pinnedAmount` or `pinnedRecurringIncome` for a pinned change.
- **The responses:** a future budget target has `carriedAmount`, `inherited`, and `pinnedAmount` when
  present. A recurring income entry has the fields that the month shows, plus
  `carriedRecurringIncome` and `hasPinnedRecurringIncome`.

### Edge cases

- **A new category, with no history.** The densify step finds no row to walk back to, so it writes
  `carriedAmount=0`. The category then appears in the current month with a `$0` target. No amount
  is ever `null`.
- **An own value equal to the inherited value.** The app permits this, and it warns the user. See
  **Update write semantics** and `matchesInheritedValue`. The server writes the value as asked.
- **An own value stays through the month change.** When a future month with its own value becomes
  the current month, the densify step finds the row already in place, and leaves it. It is then a
  normal dense row.
- **An own value on a category that the user deactivates before the future month arrives.** The
  deactivation deletes it, as above. The row can survive if the user got past the confirm
  step, for example by a direct call to the API. The densify step then ignores it when that month
  arrives, because the app does not densify an inactive category. While the month stays editable,
  no summary shows the lost row. The Spec 5 rule for an editable month shows only the active
  categories, and the categories with transactions. When that month locks, the Spec 5 rule for a
  locked month applies, and the lost budget row appears in the summary with `$0` of actuals. To
  remove it for ever, make the category active again and deactivate it in the normal way, or hard
  delete the category.
- **Two tabs that change the same thing.** The last write wins. There is no ETag and no version
  check. This is acceptable for one household.

### The generic densify step

`budget/densify.py` does not import any table class. It works on a small protocol,
`DenseSparseStore`, where each row is `(yearMonth, key, value)`, and each caller passes the
active keys:

- `BudgetDensifyAdapter(budget_table)`: the key is a `categoryId`, and the value is the carried amount.
  The cold-start value is `0`.
- `RecurringIncomeDensifyAdapter(planned_income_table)`: the key is a `plannedIncomeId`, and the
  value is the carried recurring income. It reads only the `REC#` items, so a one-time income never
  carries forward. The keys are every recurring income in the table. The store writes nothing for a
  month after the `endMonth` of the value.

`_ensure_hydrated()` and the scheduled Lambda densify both tables. `compute_summary()` and
`compute_income_plan()` use the shared walk-back functions (`walk_back_for()`,
`value_at_or_before()`).

## Income Plan and Left to Assign

### Income sources and planned incomes

An **income source** is who pays the household, such as a job, a bank, or a landlord business
(`IncomeSource-{stage}`). It holds no money. It has one or more owners, the same as an account, so
a joint source has two. A person can own any number of them, and the app does not delete a person
who still owns one. The label of a source is real data, so it lives only in DynamoDB, and never in
the code.

A **planned income** is money that one month expects (`PlannedIncome-{stage}`). It is one of two
kinds, and both have a label, a gross amount, and deductions:

- A **recurring income** (`REC#{ULID}`) repeats each month, such as a salary or the rent of one
  property. It has an income source, and one source can have many recurring incomes. It has the
  same id in each month, and obeys the month rules of a Budget row: dense past and current months,
  sparse future values, carry-forward, the grace period, the lock, the admin override, and an audit
  entry with an explanation for each change. A rename starts in the month where it is made, the same
  as a change of amount.
- A **one-time income** (`ONE#{ULID}`) is money in one month only, such as a bonus, an RSU vest, a
  tax refund, or a third paycheck. Its income source is optional. It never carries forward, and the
  densify step never copies it.

The money fields are `gross` and `deductions: [{label, kind, amount}]`. The order of the deductions
is the display order.

- The deduction kinds are `TAX`, `PRETAX_SAVINGS`, `PRETAX_BENEFIT`, and `POSTTAX_SAVINGS`. They are
  for display only. No calculation uses them.
- `net = gross − sum(deductions)`. For a recurring income, `monthlyNet = net × the times per month
  of its frequency`. The app calculates both on each read, and never stores them.
- The rules: `gross >= 0`, each deduction amount `>= 0`, `net >= 0`, and no label is empty. A
  deduction is optional, so for interest or a gift the gross amount is the net amount.
- Two recurring incomes are equal when every field is equal. A write of an equal value does
  nothing, and makes no audit entry.

The frequency of a recurring income sets the times per month that the plan counts it: weekly 4,
every two weeks 2, twice a month 2, and monthly 1. This is an estimate. A weekly income pays 5
times in some months, and an income that pays every two weeks pays 3 times in two months of each
year. Put an extra paycheck in the plan as a one-time income. The frequency is part of the value
that carries forward, so a change of frequency starts in the month where it is made.

**The end month.** A recurring income can hold `endMonth` (YYYY-MM), the last month that counts it.
A later month does not show it or count it, and the densify step does not copy it there. The end
month is part of the value, so it carries forward like the other fields. Without the admin override:

- An `endMonth` before the current month cannot be set. An `endMonth` before the month of the
  write cannot be set either.
- An end is locked once its month has passed, so the value of an ended income cannot change.
- A restart is a new recurring income, with a new id.

The admin override permits anything, also a past `endMonth`. The admin accepts the risk to these
rules.

**Why a separate table, and not more rows in `Budget-{stage}`?** The SK of the Budget table is a
`categoryId`. The `/replace` body rule, the summary, the hard delete, and the densify step all read
each row of a month as a category row. A separate table keeps each of them as it is.

### The month of an income plan

`GET /api/income/{YYYY-MM}` returns one month:

```
{
  "yearMonth": "2026-11",
  "state": "EDITABLE" | "GRACE" | "LOCKED",
  "recurring": [
    {
      "plannedIncomeId", "incomeSourceId", "label", "frequency", "gross", "deductions": [...],
      "sourceLabel", "owners", "sourceSortOrder",
      "net",                     // one payment
      "monthlyNet",              // net × the times per month of the frequency
      "carriedRecurringIncome",  // the value that carries forward, or null
      "hasPinnedRecurringIncome",        // true if the month shows a pinned value
      "inherited"                // future months only: the carried value comes from the walk back
    },
    ...
  ],
  "oneTime": [{ "plannedIncomeId", "label", "incomeSourceId"?, "gross", "deductions", "net" }, ...],
  "plannedNetIncome": number
}
```

`plannedNetIncome = sum(monthlyNet of each recurring income that counts in the month) + sum(net of each
one-time income)`. A current or past month shows each recurring income that it holds. A future month
shows each recurring income with its own value there, or a value from the walk back. A locked month uses its
own dense rows only, so its value never changes after the lock.

### Writes

`/replace` and `/update` copy the budget endpoints. The body key is `recurring`, a list of
`{action, plannedIncomeId?, recurringIncome?}`, in place of `targets`. Each body has an
`explanation`, and `/replace` takes an optional `override`.

- `SET` writes `recurringIncome`. With no `plannedIncomeId`, it creates a new recurring income from
  this month on.
- `CLEAR` removes the own carried value of a future month, so that it inherits again. It is for
  `/update` only.
- The rules of the end month apply to each change. `/update` takes an optional `override` for them.
- `SET_PIN` and `CLEAR_PIN` set or remove the value that only this month
  uses. See **Pinned values**.
- Each `plannedIncomeId` and each income source must exist.
- A new recurring income, or a move to a different income source, cannot name an inactive income
  source. A recurring income can keep its inactive income source, so `/replace` can still send it.
  The same rule applies to a one-time income. The admin override permits an inactive income source.
- `/replace` needs every recurring income with a value in that month. The response is the month.
- `/update` returns `{incomePlan: <the month>, warnings: {laterCarriedChanges, matchesInheritedValue}}`,
  the same two warnings as the budget `/update`.
- The `/one-time` route needs an `explanation`. A `plannedIncomeId` must start with `ONE#`.
- The deactivation of a source sets `endMonth` to the current month on each of its recurring incomes,
  so the current month keeps its plan. It drops their own values after the current month after a
  confirmation, the same as for a category. There is no hard delete for an income source.

### Left to assign

```
leftToAssign(M) = plannedNetIncome(M) − totals.budgeted(M)
```

`totals.budgeted` comes from the summary, so the two numbers use the same category membership.
`GET /api/summary` returns `income: {plannedNetIncome, leftToAssign}`. `GET /api/budget/{YYYY-MM}`
does not return it. The budget page fetches `GET /api/income/{YYYY-MM}`, and calculates the value
live from the form while the user types. The value never blocks a save, and no transaction changes
it. The frontend shows it in a different colour when it is not `$0`.

## Budget Treatments

Each transaction has a budget treatment (`budget/treatment.py`):

| Budget treatment | What | In the summary | Category |
|---|---|---|---|
| `BUDGET_IMPACTING` (absent means this) | a normal expense or refund | in its category | required |
| `OFF_BUDGET` | work travel that an employer pays back; a payment with money from a fund | in the Off-budget total only | optional |
| `SKIP` | paychecks, bonuses, interest, transfers, card payments | nowhere | none |

The rules for the fields of a transaction, on a create, an update, and a commit:

- `budgetTreatment = BUDGET_IMPACTING` needs a `categoryId`. `budgetTreatment = SKIP` has no `categoryId`: the server
  removes one that the client sends.
- `accountId`, if present, must be an account in the Account table.
- `note`, if present, is at most 200 characters after a trim. An empty note is removed.
- A change of the budget treatment, the category, or the note obeys the usual editability rules.

The summary counts only BUDGET_IMPACTING rows, in each category and in the membership rules. An OFF_BUDGET
row with a category does not put that category on the summary. The `/replace` body rule reads
BUDGET_IMPACTING rows only. The summary response holds these totals:

```
"totals":              { "budgeted", "actual", "delta" },   // BUDGET_IMPACTING rows only
"offBudget":           { "actual" },                        // the OFF_BUDGET rows
"totalsWithOffBudget": { "budgeted", "actual" },            // budgeted = totals.budgeted
"income":              { "plannedNetIncome", "leftToAssign" }
```

The Off-budget total is below the TOTAL, and not in it. If it were in the TOTAL, the total delta
would always show an overspend that is not real. The summary page shows the Off-budget row and the
"Total + off-budget" row below the Total row. The ledger hides SKIP rows by default.

**No migration was necessary.** An absent `budgetTreatment` reads as BUDGET_IMPACTING, and every older row has a
`categoryId`, so every older row stays valid. An older paycheck in an "Income" category stays a
BUDGET_IMPACTING row until the user changes it to SKIP. A locked month stays as it is, unless an admin uses
the override.

The hard delete of a category deletes every transaction with that `categoryId`, an OFF_BUDGET row
too. A SKIP row has no category, so the hard delete never touches it.

## Hard delete semantics

`DELETE /api/categories/{id}` means something different from a deactivation. Of all the operations
on the life of a category, it is the only one that destroys data, that no one can undo, and that
the app audits. Use it when someone made a category by mistake, and it must not appear in any
historical record. Do not use it for "I no longer want to budget this". Deactivation is for that.

### Access

Only an admin can do this. The handler checks the `budget-admin-{stage}` claim on each call. There
is no `override` flag. The admin group alone gives the right.

The frontend does not show the hard delete on the normal `CategoriesPage`. It sits behind a
separate admin tools route. The app draws that route only when the JWT carries the
`budget-admin-{stage}` claim.

### Pre-flight (dry run)

`GET /api/categories/{id}?deletion_preview=true` returns a summary of the effect. It changes
nothing:

```
{
  "categoryId": "01HQ...",
  "name": "Coffee",
  "impact": {
    "budgetRows": 47,        // total Budget rows referencing this category
    "transactions": 312,     // total Transactions referencing this category
    "lockedMonthsAffected": 18,
    "auditEntriesReferencing": 89  // not deleted, but will become unresolvable
  }
}
```

The admin tools page makes the admin read this preview first. Only then does it permit the call
that destroys the data.

### Destructive call

```
DELETE /api/categories/{id}
Body: {
  "confirm": true,
  "confirmName": "Coffee",       // must match Category.name exactly, case-sensitive
  "explanation": "..."           // required, non-empty
}
```

What the handler does:

1. It checks the `budget-admin-{stage}` claim. If the claim is absent, it returns `403`.
2. It checks three things: `confirm` is `true`, `confirmName` equals the current `Category.name`,
   and `explanation` is not empty. If any check fails, it returns `400`.
3. It then does these four steps as one operation:
   a. Find and delete every Budget row with `categoryId = id`, in all year-months.
   b. Find and delete every Transaction row with `categoryId = id`, in all year-months.
   c. Delete the Category row.
   d. Write one `CATEGORY_HARD_DELETE` entry into the audit table, which has one partition. Set
      `PK="AUDIT"`, `sortId` to a new ULID, `changedAt` to now in UTC, `effectiveYearMonth` to
      `null`, `categoryId` to `id`, and also `explanation` and `user={sub, email, username}`. Set
      `override=true`, because only an admin can do this — see **Audit entry `override` field**.
      Set `changes={budgetRowsDeleted: N, transactionsDeleted: M, name: previousName}`.
4. It returns `200`, with the counts.

DynamoDB cannot make these steps atomic across tables. The handler therefore runs them in order,
and each one may fail. It writes the audit entry last, on purpose. If that entry exists, the whole
cascade finished, unless someone changed the data by hand. If the write of the audit entry itself
fails, the app tells the admin, and someone must repair the state by hand.

### Effects on history

A hard delete breaks Spec 2 for that category, and it does so on purpose:

- **A summary of a locked month** that held this category no longer holds it. The total actuals for
  those months fall by the amounts of the deleted transactions. This is the design. The admin has
  said that the category should never have existed.
- **An audit log entry** that names the deleted `categoryId` stays. This includes a past `CREATE`,
  `UPDATE`, `CLEAR`, `PIN`, or `UNPIN` entry for that category. The app can no longer find the name, so the UI shows
  `<deleted category {id}>`. A hover shows the original `Category.name`, which the app reads from
  the `changes.name` field of the `CATEGORY_HARD_DELETE` entry.
- **A transaction in a locked month** is deleted. This is the hardest part of the cascade. It is
  also the main reason to keep the hard delete for "this category should never have existed".

### Why not orphan transactions instead?

We thought about the other way: to set `categoryId = null` on each Transaction, and to keep the
rows. We rejected it for two reasons. First, it makes a state with no category, and then every
other part of the app must guard against it, in the summary totals, in the `/replace` checks, and
elsewhere. Second, it does not help the person who needs it. If the admin wants to keep the
transactions under a different category, the admin must move each one to a real category first,
and then hard delete a category that is empty. The present design makes that step necessary.

## Admin Operating Model

This section covers the whole experience of work as an admin. It shows how a person becomes an
admin, how an admin turns admin mode on in the frontend, what each kind of change looks like in
that mode, and what the admin tools page holds. For the API rules on the `override` flag and the
JWT claim, see **Editability and the Admin Override > Admin override**, above.

### Bootstrap

The CDK makes the `budget-admin-{stage}` Cognito group in the stack that holds the user pool. The
group is **empty after a deploy**. The person who deploys adds themselves, and anyone else who
needs admin rights, with this command:

```
aws cognito-idp admin-add-user-to-group \
  --user-pool-id <pool-id> \
  --username <email> \
  --group-name budget-admin-{stage}
```

The new admin must sign out and sign in again. The new JWT then carries the `cognito:groups` claim.
The app has no page to give or to remove admin rights. This happens rarely, so the AWS CLI stays
the correct tool for it.

### Session model

The app draws a toggle in the header **only when the JWT carries `budget-admin-{stage}`**. It has
two states: Off, which is the default, and On.

- **It starts Off** on each page load, in each new tab, and after each sign-in. The state belongs
  to **one tab**. It lives in `useState` only. The app never puts it in `localStorage`, in
  `sessionStorage`, or in a cookie.
- **It has no timeout.** The state holds until the user turns it off, closes the tab, or reloads
  the page. The colours below are the safeguard.
- One click turns it on, and one click turns it off. There is no second sign-in, and no confirm
  box. The friction belongs to each action instead.

### Visual treatment

When admin mode is On:

- A banner stays across the top of the window: `Admin mode active — your changes can bypass lock
  and grace rules. [Turn off]`.
- The banner uses a warning colour, amber or red.
- The header and the navigation strip take the same colour. Admin mode is then clear on every page.
- An **Admin tools** item appears in the navigation.

When admin mode is Off, there is no banner, no colour on the header, and no admin tools item.

The banner and the colour are the only guard against "I forgot that it was on". They are strong on
purpose.

### Per-action behavior under admin mode

For each kind of change, this section shows three things: what the UI shows when admin mode is Off
and the lock stops the action; what it shows when admin mode is On and the action can go ahead; and
whether the app asks for an explanation.

| Action | Off (locked context) | On (admin mode) | Explanation collected |
|--------|---------------------|-----------------|----------------------|
| `POST /api/budget/{YYYY-MM}/replace` on a locked month | Form is read-only; no Save button. | Form is editable. Save button reads "Save with override". | Yes — same `explanation` field the form requires for every `/replace`, written to the audit log with `override=true`. |
| `POST /api/budget/{YYYY-MM}/update` on a locked month | Future-month controls hidden. | Future-month controls visible. Save button reads "Save with override". | Yes — same `explanation` field, written to the audit log with `override=true`. |
| `POST /api/transactions` with date in locked month | "Save" disabled with tooltip explaining lock. | "Save" enabled, label reads "Save (override)". | No — transactions are not audited; no explanation field is shown. |
| `POST /api/transactions/update` moving a row into/out of a locked month | "Save" disabled. | "Save" enabled, label reads "Save (override)". | No. |
| `POST /api/transactions/delete` on a row in a locked month | "Delete" disabled. | "Delete" enabled, label reads "Delete (override)". | No. |
| `POST /api/transactions/commit` with any locked-month rows | Locked rows surface as inline errors; Commit button disabled. | Locked rows become inline warnings (different color). A persistent header on the review screen reads: "Committing N rows in locked months: [Mar 2024, Apr 2024] via admin override." Commit button reads "Commit (override)". | No. |
| `DELETE /api/categories/{id}` | Not reachable from any page. | Reachable only via Admin tools. Form on its own page; not gated by the session toggle. | Yes — typed-name confirmation + required explanation, written to the `CATEGORY_HARD_DELETE` audit entry. |

A budget target needs an explanation, and a transaction does not. This difference follows directly
from the audit scope. It is of no use to ask the user for a reason that the app then throws away.

### Admin tools page

The page sits at `/admin`. The app draws it only when the JWT carries `budget-admin-{stage}`. To
open `/admin`, the toggle does **not** need to be On. Each admin tool carries its own safeguards —
see **Hard delete semantics**.

What the page holds at the start:

- **Hard delete a category.** A list of every category, active and inactive. On a choice, the page
  calls `GET /api/categories/{id}?deletion_preview=true`, and shows the counts. It also has a field
  for the typed name, a field for the explanation, and a red "Permanently delete" button.

Space for more tools later, but not in v1:

- A button to densify the current month by hand.
- The health of the cron job: the time of its last good run.
- A query on the raw audit log, by user, by date range, and by action.

### Stage isolation

A user in `budget-admin-dev` is an admin in dev only. To make that user an admin in prod, someone
must add them to `budget-admin-prod` as well. The server enforces this. The Lambda reads its
`STAGE` environment variable on each request. It then rejects an override or a hard delete when the
JWT does not carry the group for that stage.

The frontend builds the same group name from a stage value. The deploy puts that value into
`config.json`. The admin toggle and the `/admin` route therefore appear only when the user holds
the claim for that stage.

## Accounts for the Budget

The CSV import uses two optional fields of an account, which the user sets on the Accounts page:

- `cashFlowRole`: `SPENDING`, `HOLDING`, `EXTERNAL`, or `NON_CASH`. Absent means the default for the
  account type, so every account has a role.
- `accountNumberLastFour`: a list of 4-digit strings, the last four digits of the account number.
  The import uses them to find the other side of a transfer.

| Account type | Default cash flow role |
|---|---|
| `checking`, `credit_card` | SPENDING: expenses are paid from it |
| `savings`, `brokerage`, `529`, `530a`, `401k`, `403b`, `roth_ira`, `traditional_ira`, `hsa`, `other_asset` | HOLDING: money is kept in it |
| `mortgage`, `other_liability` | EXTERNAL: money that goes to it leaves the household |
| `real_estate`, `vehicle` | NON_CASH: it holds value, but no money moves in or out |

Only a SPENDING or HOLDING account can take a CSV upload.

**The `credit_card` type and the `debt` class.** A credit card is a liability with the fixed asset
class `debt`, and it has no loan terms. `debt` is the fixed class of every liability: `mortgage`,
`other_liability`, and `credit_card`. An asset account can never choose it. The user can set
`excludedFromNetWorth` on a credit card, to keep its balance out of the net worth.

Before the `debt` class, a liability had the fixed class `other`. The one-time script
`lambda/scripts/migrate_budget_income.py` moves the older data. It does a dry run by default, writes
only with `--apply`, and is safe to run again. It changes `assetClasses` and the snapshot values of
each mortgage and other liability from `other` to `debt`. With `--to-credit-card <accountId>`, it
also changes that account to the `credit_card` type, and removes its loan terms. The same script
renames the amounts of each Budget row (`amount` → `carriedAmount`, `onlyAmount` → `pinnedAmount`,
and a `null` amount → `0`). It does not change the audit log: the old entries use the old names, and
are deleted by hand.

## LLM-Driven CSV Import (`budget/csv_import/`)

Each upload belongs to one **account**, which the user chooses. The account needs the cash flow role
SPENDING (checking, a credit card) or HOLDING (a savings fund, a brokerage, an IRA). See **Accounts
for the Budget**.

The upload sends as little data as possible to the Anthropic API. Each call reads the API key from
SSM:

1. **Find the columns.** `detect_column_types()` finds a type for each column locally: date,
   number, or text. `map_columns()` sends the header row and the types, and no cell values. The LLM
   returns `date`, `description`, `amount`, an optional `bankCategory`, and an optional
   `amount_invert`.
2. **Set the sign.** The app stores money out as positive. After the LLM's `amount_invert`, a
   local check flips every sign when most rows of a spending account came in negative
   (`spending_account_needs_flip()`). The "flip all signs" button on the review screen sends the
   file again with `flipSigns: true`. The response reports all three in `signCheck`.
3. **Give a kind and a category.** `categorize_transactions()` sends each unique description one
   time, with the active category names. Each LLM call holds at most 50 descriptions. It returns a
   `kind` (expense, refund, income, card_payment, own_transfer) and a category for each one, and
   maps them back to every row. An answer that the token limit cuts off is an error.
4. **Suggest a budget treatment.** The import finds the other side of each row (see below). The
   direction table then gives the budget treatment. A transfer between two own accounts counts one time
   at most, on the side where the money leaves a spending account.
5. **Mark duplicates.** Each row gets a fingerprint (see below). A row whose fingerprint is in the
   table already is a duplicate.

The upload loads the saved transactions of each upload month, and of the month before and after
it, for the duplicates and the pair match. The bank category and the amounts never go to the LLM.

### The other side of a row

After the sign step, an amount is positive for money out and negative for money in, from the point
of view of the upload account. The other side is the outside world, or one of the user's own
accounts. These checks run in order, and the first one with an answer wins. Each row records the
check in `suggestedBy`:

1. **PAIR.** A saved transaction in a different account, with the opposite sign, the same absolute
   amount, and a date at most 4 days away. With one candidate, the other side is that account. With
   more than one, the row is marked, and the import does not choose. The same upload cannot hold
   the other side, because all its rows belong to one account.
2. **ACCOUNT_NUMBER.** A 4-digit group in the description that is in the `accountNumberLastFour` of
   exactly one other account.
3. **BANK.** The bank category of the row, from a short list of generic bank words, such as
   "payroll", "transfer", or "card payment".
4. **LLM.** The kind that the categorization gave the description.
5. **DEFAULT.** No check gave an answer.

An EXTERNAL or NON_CASH account, such as a mortgage, counts as the outside world.

### The direction table

| Upload role | Money | Other side | Suggested budget treatment | Mark for review? |
|---|---|---|---|---|
| SPENDING | out | outside | BUDGET_IMPACTING, LLM category | only if no category |
| SPENDING | out | SPENDING account (pair, account number) | SKIP | no |
| SPENDING | out | HOLDING account (pair, account number) | BUDGET_IMPACTING, LLM category | yes, if the LLM gave no category |
| SPENDING | out | own account, role unknown, LLM says `card_payment` | SKIP | no |
| SPENDING | out | own account, role unknown, LLM says `own_transfer` with a category | BUDGET_IMPACTING, that category | **yes** |
| SPENDING | out | own account, role unknown, no category | SKIP | **yes** |
| SPENDING | in | outside, LLM says `refund` | BUDGET_IMPACTING, LLM category | only if no category |
| SPENDING | in | outside, income | SKIP | no |
| SPENDING | in | any own account | SKIP | no |
| HOLDING | out | outside | OFF_BUDGET | no |
| HOLDING | out | any own account | SKIP | no |
| HOLDING | in | anything | SKIP | no |
| any | any | unknown | SKIP, reason `unknown_direction` | **yes** |

When the import is not sure, it suggests SKIP and marks the row. A missed savings transfer shows as
a `$0` actual in a savings category, and the user sees it. A double count is hard to see.

### Fingerprints

```
fingerprint = sha256(accountId | transactionDate | amount | normalizedDescription | n)
```

- `normalizedDescription` is the description in upper case, with each run of white space replaced
  by one space.
- `n` is the count of earlier rows in the same file with the same first four values. Two real
  coffees on the same day, for the same amount, then get different fingerprints.
- A row from before fingerprints has none, so it never matches. The first upload of a date range
  that overlaps an older upload can therefore make a copy. The user must look for this one time.

### Upload response shape

```
{
  "accountId": "01J...",
  "signCheck": { "llmInverted": bool, "locallyFlipped": bool, "userFlipped": bool },
  "columnMapping": { "date", "description", "amount", "bankCategory"?, "amount_invert"? },
  "transactions": [
    {
      "transactionDate", "description", "amount", "categoryId", "categoryName", "accountId",
      "budgetTreatment": "BUDGET_IMPACTING" | "OFF_BUDGET" | "SKIP",
      "suggestedBy": "PAIR" | "ACCOUNT_NUMBER" | "BANK" | "LLM" | "DEFAULT",
      "pairWith": { "yearMonth", "sortId" } | null,
      "fingerprint": "…",
      "duplicate": bool
    }
  ],
  "validations": [
    { "index": 0, "issues": ["missing_category", "locked_month"],
      "review": ["no_pair_found", "llm_guess", "many_pairs", "unknown_direction"] }
  ]
}
```

An **issue** blocks the commit. `missing_category` applies to a BUDGET_IMPACTING row with no known category,
and to an OFF_BUDGET row with an unknown category. `locked_month` applies to a row in a LOCKED
month. A **review reason** does not block the commit. It puts the row in the "Look at these first"
list of the review screen.

A CSV row with too few columns, or an amount that is not a number, is **dropped with no error**.
If no row is left, the upload returns `400`. If the LLM cannot find the three columns, the upload
also returns `400`. The upload stores the date text of the bank as it is, and the year-month is
its first 7 characters. A bank date format such as `03/15/2026` therefore gives a wrong year-month.
This is a known limit.

The review screen shows three lists: the rows to look at first, the BUDGET_IMPACTING rows, and the
OFF_BUDGET and SKIP rows. A duplicate row starts unselected. The user can change the budget treatment,
the category, and the note of each row before the commit.

### CSV commit semantics

`POST /api/transactions/commit` takes the whole batch in one request. At the API level, the commit
is all or nothing:

```
POST /api/transactions/commit
Body: {
    "rows": [{ "transactionDate", "description", "amount", "categoryId" }, ...],
    "override": false
}
```

What the handler does:

1. It runs `editability(row.transactionDate.yearMonth, now)` for every row.
2. If any row is LOCKED, and no override applies, the response is `409 Conflict`. It reports the
   fault on each row, and the app **writes no rows**. An override does not apply when the client
   did not ask for one, or when the JWT has no `budget-admin-{stage}` claim.
3. If every row passes, or an override applies, the app writes every row. It writes to the
   Transactions table with `BatchWriteItem`. If the retries for a part-written batch run out, the
   request returns `500`.

**A retry is safe for CSV rows.** Just before it writes, the commit loads the months of the batch
and drops each row whose fingerprint exists, and it lists them in `skippedDuplicates`. A retry
after a `500` therefore writes only the rows that are not there yet. Two commits at the same time
can still both write; that is acceptable for one household. A row with no fingerprint (a manual
row, or a row from before fingerprints) is never dropped.

The older path, `POST /api/transactions`, stays. The UI uses it to add one transaction. It is not
the path for a CSV commit.

## Environment Variables

### Budget Handler Lambda

```
+-----------------------------+-----------------------------------+
| Variable                    | Purpose                           |
+-----------------------------+-----------------------------------+
| STAGE                       | "dev" or "prod"; builds the admin |
|                             | group name                        |
| CATEGORY_GROUP_TABLE_NAME   | DynamoDB CategoryGroup table      |
| CATEGORY_TABLE_NAME         | DynamoDB Category table           |
| BUDGET_TABLE_NAME           | DynamoDB Budget table             |
| TRANSACTIONS_TABLE_NAME     | DynamoDB Transactions table       |
| BUDGET_AUDIT_LOG_TABLE_NAME | DynamoDB audit log table          |
| PROFILE_TABLE_NAME          | DynamoDB Profile table            |
| ACCOUNT_TABLE_NAME          | DynamoDB Account table            |
| NETWORTH_SNAPSHOT_TABLE_NAME| DynamoDB NetWorthSnapshot table   |
| INCOME_SOURCE_TABLE_NAME    | DynamoDB IncomeSource table       |
| PLANNED_INCOME_TABLE_NAME   | DynamoDB PlannedIncome table      |
| SSM_API_KEY_PARAM           | SSM path to the Anthropic API key |
+-----------------------------+-----------------------------------+
```

The scheduled densify Lambda gets `CATEGORY_TABLE_NAME`, `BUDGET_TABLE_NAME`,
`PLANNED_INCOME_TABLE_NAME`, and `STAGE`. It can read the Category table, and it can read and write
the Budget and PlannedIncome tables.

Every environment variable goes through `get_env_var()`. That function raises an error if the value
is absent or blank.

## Frontend (`budget/`)

React and TypeScript, built by Vite.

The pages are `SummaryPage`, `BudgetPage`, `IncomePage`, `TransactionsPage`, `UploadPage`,
`CategoriesPage`, `AuditLogPage`, `AdminToolsPage`, and `CallbackPage`. `IncomePage` shows the
recurring and one-time incomes of a month, with the breakdown of each one, and the income sources.
The summary page and the budget page show the shared `LeftToAssign` line at the top. The net worth record adds `AccountsPage` and
`NetWorthEntryPage`. `App.tsx` holds the routes. `components/Layout.tsx` holds the navigation, with
one list of links for the budget (`BUDGET_LINKS`) and one for the net worth record.

`types.ts` holds the response types. `parsers.ts` checks each response at run time before the
page uses it, so a new field in a response needs a change in both files.

The shared parts are `auth.ts`, which holds the PKCE flow and permits one token request at a time,
and `api.ts`, which holds the typed wrappers. The shared components are `MonthPicker`,
`LoadingOverlay`, `StatusMessage`, `DevBanner`, and `AdminBanner`. `AdminBanner` is the banner that
stays on the screen while admin mode is on.

The CDK stack sends the build output to S3 with a `BucketDeployment`. That step also writes a
`config.json` beside it, which holds the Cognito domain, the client ID, and the stage. One bundle
therefore works in every stage. The stage value is what lets the frontend build the
`budget-admin-{stage}` group name it expects.

### Own-value indicator

For any year-month later than the current one, the server adds an `inherited: bool` flag to each
summary row and each budget row. The frontend draws a row with its own value (`inherited: false`)
differently, with a coloured dot or a chip. The user can then see at once which targets hold, and
which ones carry forward (Spec 17). When such a year-month becomes the current, grace, or locked
month, the server stops sending the flag. The row is then a normal dense row.

When the user saves a future value, the response holds `warnings.laterCarriedChanges` and
`warnings.matchesInheritedValue`. The UI shows a box with these warnings, and two buttons: "Keep
them" and "Clear the later values". It reports success only after the user chooses.

### Audit log page

`AuditLogPage` calls `GET /api/audit-log?limit=N`. The user sets `N` in the UI. The default is
`10`, and the server permits no more than `200`. The page shows the most recent entries, and it
shows the `override` flag on each one.

## Testing

The Python unit tests are in `lambda/tests/`. These files hold the budget tests:
`test_personal_finance_handler.py`, `test_budget_auth.py`, `test_budget_categories.py`,
`test_budget_category_groups.py`, `test_budget_budget.py`, `test_budget_densify.py`,
`test_budget_editability.py`, `test_budget_transactions.py`, `test_budget_summary.py`,
`test_budget_audit.py`, `test_budget_csv_import.py`, `test_budget_treatment.py`,
`test_budget_income.py`, and `test_budget_directions.py`. The net worth tests are in
`test_networth_*.py`. Every DynamoDB call and every Anthropic call is a mock (`MagicMock`). Run
`uv run pytest` from `lambda/`.

The CDK tests are in `infra/test/`. Run `npm test` from `infra/`.

## Key Design Decisions

- **An enum for each fixed set of values** — A set of strings that the app stores or sends is a
  `StrEnum`, such as `BudgetTreatment`, `EditabilityState`, `AuditAction`, `CashFlowRole`,
  `PlannedIncomeKind`, `RecurringIncomeAction`, `PayFrequency`, `DeductionKind`, `TransactionKind`, `SuggestedBy`,
  `ReviewReason`, and `RowIssue`. A member is a `str`, so it compares equal to the stored string,
  and JSON and boto3 write its value. A set that never leaves the process is a plain `Enum`, such
  as `BankHint` and `SideKind`.

- **A plan-based budget** — Left to assign = planned net income − targets. The user wants a stable
  monthly plan. A cash-based plan needs work on each payday. The plan counts the same number of
  payments every month, so extra money (a third paycheck, a bonus, an RSU or DCFSA payout) is not in
  the plan unless the user adds a one-time income. The app shows no surplus for it, and gives no
  warning. The app does not track actual income, so a paycheck deposit is SKIP, and there is no
  "income" budget treatment.
- **Gross amount and each deduction are recorded** — The user can then see why the net amount is
  the number that it is. The app does not model a change of the net amount during the year, such as
  the 401k limit or the Social Security wage limit. The user changes the recurring income by hand.
- **A DCFSA is only a pre-tax deduction** — It has no link to a category, because the user can
  spend DCFSA money on any qualified expense.
- **No category balances** — No envelopes and no carryover. Money counts one time, when it goes into
  a fund. Money that leaves a fund is Off-budget.
- **The app saves every row of a CSV import, SKIP rows too** — The fingerprints and the pair match
  need them, and the user can change a row later.
- **No real data in the code** — The repository is public. Employer names, labels, and amounts live
  only in DynamoDB.
- **Import rules, remembered choices, and a stored column mapping are deferred** — They can fit in
  later as one more step before the LLM step.
- **The income plan obeys the budget's month rules** — A locked month shows the same left to
  assign amount for ever. One generic densify step serves both tables.
- **One budget treatment for "the budget must not count this"** — Off-budget covers work travel and
  payments from a fund. Skip covers money that is not an expense. Only Budget rows are actuals.
- **The boundary rule for the CSV import** — Money counts only when it crosses the boundary
  between the outside world, the spending accounts, and the holding accounts. When unsure, the
  import suggests Skip and marks the row, because a double count is harder to see than a missed
  transfer.
- **As little data to the LLM as possible** — Header names and column types for the mapping, and
  unique descriptions for the categories. No amounts, dates, or account names.

- **Two stacks** — The DynamoDB tables stay apart from the web stack. The data then survives a
  deploy of the app, and the prod removal policy can be `RETAIN` without a tie to the Lambda.
- **An amount is an integer in millionths** — Full precision, and no floating-point drift.
- **A ULID for each identifier** — It sorts by the time of creation, it hides nothing in a URL, and
  two writers need no agreement to make one.
- **A sparse future, and a dense present and past** — A value that the user wrote is the only
  Budget row that exists for a future month. The densify step writes the dense rows at the month change, from the cron job or
  from the fallback. The future therefore stays live, and it follows a change in an earlier month.
  A locked month stays a complete record on its own, and it needs no walk-back when the app shows
  it.
- **`POST` for every change, except the hard delete** — `POST` for a budget write, through
  `/replace` or `/update`, and for a transaction write. The body then survives CloudFront, and every
  request has one shape. The hard delete keeps the `DELETE` verb on purpose. It destroys data and
  only an admin can do it, so it deserves the verb that shows danger, although that breaks the rule
  used everywhere else.
- **OAuth through CloudFront** — The Cognito hosted UI comes from the app's own origin, so a
  request for a new token is same-origin and needs no CORS.
- **The PKCE flow** — No client secret in the browser. On a 401 the app gets a new token, and it
  permits only one such request at a time.
- **An LLM finds the columns** — It reads a CSV from any bank, and the app needs no parser for each
  one.
- **An explanation for each budget change** — Every change writes an audit entry with a reason that
  a person can read.
- **Deactivation keeps the history, and a hard delete destroys it** — These are two different
  operations, and not two forms of one. Deactivation is the normal path for "I no longer want to
  budget this". It keeps every Budget row and every Transaction, it drops the future values when the
  user confirms, and the user can undo it. The hard delete is the admin operation for "this
  category should never have existed". It deletes every Budget row and every Transaction in all
  year-months, including the locked ones. It writes a `CATEGORY_HARD_DELETE` audit entry, and it
  breaks Spec 2 for that category on purpose. It needs the `budget-admin-{stage}` group claim and a
  typed name, and it sits behind the admin tools route. The normal categories page cannot reach it.
- **The server computes editability** — The app works out the lock state and the grace state from
  ET, on each request that changes data. A client can therefore never make the backend write to a
  locked month. The `budget-admin-{stage}` group claim is the only way to get an override, and the
  override flag means nothing without that claim.
- **One admin group for each stage** — `budget-admin-dev` and `budget-admin-prod` are two separate
  Cognito groups. An admin in dev is therefore not an admin in prod, even for the same person. This
  friction is deliberate, and someone must add the user to each group.
- **The CSV commit checks the whole batch first** — The endpoint checks every row before it writes
  any row, so no row for a locked month gets in. A failure part way through can leave some rows
  written, but a retry is safe: the fingerprints drop each row that the first try wrote.
- **The audit log holds budget targets only** — The app does not audit a change to a transaction.
  The schema stays simple, and it matches how the user reviews the data. What matters is that
  *budget against actual* is correct for a past month, and not a record of who edited which
  transaction.
- **A history of each category name** — A rename is always permitted, and it adds to the
  `nameHistory` list on the category row. A locked month can then find the name that was in use at
  the time. No Budget row and no Transaction row needs a copy of the name.
