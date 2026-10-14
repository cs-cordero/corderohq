# Architecture - Personal Finance App

## What This Project Does

A finance tool for the family. It has two features today: a monthly household budget, and a net
worth record.

For the budget, a user signs in and sets a target for each category in a month. The user then
uploads a CSV export from a bank or a credit card, and the app fills in the transactions. The user
can then compare the actual amounts against the budget. The app records each budget change in an
audit log, and each change needs an explanation.

For a CSV import, an LLM finds the date column, the description column, and the amount column. It
then gives a category to each transaction, from the categories the user has active.

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
    budget/csv_import.py   LLM-driven CSV column mapping + categorization
    aws/dynamodb.py        Table wrappers and summary computation
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
| Category-{stage}       | categoryId         | —          | categoryId is ULID. Fields: |
|                        |                    |            | name, active, timestamps,   |
|                        |                    |            | nameHistory (list of        |
|                        |                    |            | {previousName, replacedAt   |
|                        |                    |            | ISO-8601 UTC}). Soft-delete |
|                        |                    |            | via active=False. Names are |
|                        |                    |            | unique case-insensitive     |
|                        |                    |            | across active + inactive —  |
|                        |                    |            | see **Category name        |
|                        |                    |            | uniqueness** below.         |
+------------------------+--------------------+------------+-----------------------------+
| Budget-{stage}         | yearMonth          | categoryId | yearMonth is YYYY-MM.       |
|                        |                    |            | amount (millionths). One    |
|                        |                    |            | row per category per month. |
+------------------------+--------------------+------------+-----------------------------+
| Transactions-{stage}   | yearMonth          | sortId     | sortId = {transactionDate}# |
|                        |                    |            | {ULID}. Fields:             |
|                        |                    |            | transactionDate,            |
|                        |                    |            | description, amount         |
|                        |                    |            | (signed millionths;         |
|                        |                    |            | positive = expense,         |
|                        |                    |            | negative = income or        |
|                        |                    |            | return), categoryId. Sort   |
|                        |                    |            | key sorts chronologically   |
|                        |                    |            | within a month.             |
+------------------------+--------------------+------------+-----------------------------+
| BudgetAuditLog-{stage} | "AUDIT"            | sortId     | Single-partition design so  |
|                        |                    |            | recent-N queries are one    |
|                        |                    |            | Query with                  |
|                        |                    |            | ScanIndexForward=false. PK  |
|                        |                    |            | is the literal string       |
|                        |                    |            | "AUDIT". sortId is ULID     |
|                        |                    |            | (sortable by time). Fields: |
|                        |                    |            | changedAt (ISO-8601 UTC     |
|                        |                    |            | timestamp), effectiveYear-  |
|                        |                    |            | Month (nullable),           |
|                        |                    |            | categoryId, action (CREATE/ |
|                        |                    |            | UPDATE/PIN/UNPIN/CATEGORY_  |
|                        |                    |            | HARD_DELETE), explanation,  |
|                        |                    |            | user ({sub, email,          |
|                        |                    |            | username}; see **Audit user |
|                        |                    |            | identity**), override       |
|                        |                    |            | (bool), changes (shape      |
|                        |                    |            | varies by action). CREATE/  |
|                        |                    |            | UPDATE come from /replace   |
|                        |                    |            | on dense months; PIN/UNPIN  |
|                        |                    |            | from /pin OR from           |
|                        |                    |            | deactivation dropping       |
|                        |                    |            | future pins; CATEGORY_HARD_ |
|                        |                    |            | DELETE from DELETE          |
|                        |                    |            | /api/categories/{id} with   |
|                        |                    |            | effectiveYearMonth null.    |
+------------------------+--------------------+------------+-----------------------------+
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
| GET    | /api/categories/{id}?deletion_preview=   | Admin-only. Returns the hard-      |
|        | true                                     | delete impact summary (counts of   |
|        |                                          | Budget rows, Transactions, locked  |
|        |                                          | months affected, audit entries     |
|        |                                          | that will become unresolvable).    |
|        |                                          | See **Hard delete semantics**.     |
+--------+------------------------------------------+------------------------------------+
| POST   | /api/categories                          | Create category. Body requires     |
|        |                                          | name and initialTarget (non-zero,  |
|        |                                          | non-negative integer millionths).  |
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
|        |                                          | affectedMonths lists future months |
|        |                                          | with an explicit pin for this      |
|        |                                          | category. Body {confirm: bool}.    |
|        |                                          | If confirm=true, the deactivation  |
|        |                                          | batch-deletes those future pins    |
|        |                                          | and writes one UNPIN audit entry   |
|        |                                          | per dropped pin. If confirm is     |
|        |                                          | absent or false and                |
|        |                                          | affectedMonths is non-empty,       |
|        |                                          | returns 409 so the UI can prompt.  |
+--------+------------------------------------------+------------------------------------+
| POST   | /api/categories/{id}/reactivate          | Reactivate a deactivated category. |
|        |                                          | Future pins dropped at             |
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
|        |                                          | the current category name, and    |
|        |                                          | explanation. Hidden from the       |
|        |                                          | normal categories page. See        |
|        |                                          | **Hard delete semantics**.         |
+--------+------------------------------------------+------------------------------------+
| GET    | /api/budget/{YYYY-MM}                    | Get targets for a month. For dense |
|        |                                          | months (locked/grace/current),     |
|        |                                          | returns the stored rows directly.  |
|        |                                          | For future months, returns         |
|        |                                          | resolved effective values per      |
|        |                                          | category with a `pinned: bool`     |
|        |                                          | flag indicating whether the value  |
|        |                                          | came from an explicit pin at this  |
|        |                                          | month or from walking back. Lazy   |
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
| POST   | /api/budget/{YYYY-MM}/pin                | Future-month write. Body lists     |
|        |                                          | only the categories the user is    |
|        |                                          | pinning or unpinning. Upsert       |
|        |                                          | semantics; categories omitted      |
|        |                                          | from the body are untouched.       |
|        |                                          | `amount: null` clears a pin.       |
|        |                                          | Returns downstreamPins and         |
|        |                                          | pinMatchesCarriedValue warnings    |
|        |                                          | per category — see **Pin write    |
|        |                                          | semantics**. Writes per-category  |
|        |                                          | audit entries with action=PIN or   |
|        |                                          | action=UNPIN.                      |
+--------+------------------------------------------+------------------------------------+
| GET    | /api/transactions?month=YYYY-MM          | List transactions for a month.     |
+--------+------------------------------------------+------------------------------------+
| POST   | /api/transactions                        | Create one transaction.            |
+--------+------------------------------------------+------------------------------------+
| POST   | /api/transactions/upload                 | Body {csvData}. Returns parsed +   |
|        |                                          | auto-categorized transactions plus |
|        |                                          | per-row validation annotations     |
|        |                                          | (editability, missing category)    |
|        |                                          | for review. Nothing is committed.  |
+--------+------------------------------------------+------------------------------------+
| POST   | /api/transactions/commit                 | Body {rows, override?}. Atomic     |
|        |                                          | batch insert; rejects entire batch |
|        |                                          | if any row falls in a locked       |
|        |                                          | month without an effective         |
|        |                                          | override.                          |
+--------+------------------------------------------+------------------------------------+
| POST   | /api/transactions/update                 | Update mutable fields              |
|        |                                          | (description, amount, categoryId). |
+--------+------------------------------------------+------------------------------------+
| POST   | /api/transactions/delete                 | Body {items: [{yearMonth, sortId}, |
|        |                                          | ...]}. Batch delete.               |
+--------+------------------------------------------+------------------------------------+
| GET    | /api/summary?month=YYYY-MM               | Per-category budgeted vs. actual + |
|        |                                          | totals.                            |
+--------+------------------------------------------+------------------------------------+
| GET    | /api/audit-log?limit=N                   | Recent audit entries. Default N=10,|
|        |                                          | server cap N=200.                  |
+--------+------------------------------------------+------------------------------------+
```

A change to a transaction uses `POST`, and not `PUT` or `DELETE`. A CloudFront cache policy can
remove the body from the other two methods.

Every response carries strict security headers: `HSTS`, `X-Content-Type-Options`,
`X-Frame-Options`, and `Cache-Control: no-store`.

Six endpoints have an **editability gate**: `POST /api/budget/{YYYY-MM}/replace`,
`POST /api/budget/{YYYY-MM}/pin`, `POST /api/transactions`, `POST /api/transactions/update`,
`POST /api/transactions/delete`, and `POST /api/transactions/commit`. Each one accepts an optional
`override: true` flag in the body. The handler obeys that flag only if the JWT shows that the user
is in the admin group — see **Editability and the Admin Override**. If it does not, the handler
returns `403`. The flag does nothing on a `GET` endpoint, because any signed-in user may read
without limit. The other endpoints that change data — `POST /api/categories`, the
`PUT /api/categories/{id}` rename, `/deactivate`, and `/reactivate` — have no editability gate,
and they do not accept `override`. `DELETE /api/categories/{id}` is admin-only through a different
route, with no `override` flag. See **Hard delete semantics**.

A write to a budget target makes an audit log entry. This applies to `/replace`, to `/pin`, and to
`/deactivate`. `/deactivate` makes an entry only when it drops a future pin, and it then writes one
`UNPIN` entry for each pin it drops. `DELETE /api/categories/{id}` also writes one
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

`EDITABLE` covers the current year-month and the next 12. The `/pin` endpoint adds a stricter rule
on top: the month must be `EDITABLE` **and** later than `current_ym`. So `/pin` rejects the current
month and the grace month, although both are editable, and the user goes to `/replace` instead.

The month changes at **midnight ET on day 1** of the new month, whatever the daylight saving
offset. The grace period ends at **midnight ET on day 8** of the new month.

### Enforcement

| Endpoint | Editability check |
|----------|------------------|
| `POST /api/budget/{YYYY-MM}/replace` | `{YYYY-MM}` must be the current month, GRACE, or — via override — LOCKED |
| `POST /api/budget/{YYYY-MM}/pin` | `{YYYY-MM}` must be strictly in the future — i.e., EDITABLE and `yearMonth > current_ym` (one of the next 12 months). Pinning the current month, the grace month, or any locked month is rejected with `409`; those months are dense and `/replace` is the right endpoint. Override does not unlock `/pin` for non-future months — it has no use here |
| `POST /api/transactions` | `transactionDate`'s year-month must be EDITABLE or GRACE |
| `POST /api/transactions/update` | Both the old and the new `transactionDate`'s year-months must be EDITABLE or GRACE (an update that moves a row across months must satisfy both) |
| `POST /api/transactions/delete` | Each item's `yearMonth` must be EDITABLE or GRACE |
| `POST /api/transactions/commit` | Every row's `transactionDate` year-month must be EDITABLE or GRACE; see **CSV commit semantics** |

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
4. For a write to a budget target, through `/replace` or `/pin`, it sets `override: true` on every
   audit entry that the request makes.

The hard delete, `DELETE /api/categories/{id}`, uses the same group claim. It does not use the
`override` flag, because only an admin may do it in any case. See **Hard delete semantics**.

**Admin Operating Model**, below, covers how an admin works day to day: how to make the first
admin, the toggle in the frontend, what each action does, and the admin tools page.

### Audit scope

Recorded in `BudgetAuditLog-{stage}`:

- `POST /api/budget/{YYYY-MM}/replace` → one `CREATE` or `UPDATE` entry for each category.
  `/replace` cannot remove a row by any correct use, because the body must hold every row that
  exists. It therefore never makes a `DELETE` entry. Three other paths delete a budget row, and
  each writes a different entry: `/pin` with `amount: null` writes one `UNPIN` entry for each pin
  it clears; a deactivation that drops a future pin writes one `UNPIN` entry for each drop; and a
  hard delete removes every row for the category and writes one `CATEGORY_HARD_DELETE` entry.
- `POST /api/budget/{YYYY-MM}/pin` → one `PIN` or `UNPIN` entry for each category.
- `POST /api/categories/{id}/deactivate`, when `confirm` is `true` and `affectedMonths` holds at
  least one month → one `UNPIN` entry for each pin it drops. The budget-target audit log therefore
  stays complete, even when the app drops a pin as a side effect. The deactivation itself makes no
  entry.
- `DELETE /api/categories/{id}` → one `CATEGORY_HARD_DELETE` entry. This breaks the rule that the
  app does not audit the life cycle of a category. It breaks it because no one can undo a hard
  delete, and the delete destroys historical data. The entry is then the only trace left of the
  category and of the action.

### Audit entry `changes` payload

The shape of the `changes` field on a `BudgetAuditLog-{stage}` row depends on the action:

- `CREATE` — `/replace` wrote a new Budget row.
  `{amount: {before: null, after: number | null}}`.
- `UPDATE` — `/replace` changed the amount on a Budget row that existed.
  `{amount: {before: number | null, after: number | null}}`.
- `PIN` — `/pin` wrote a future pin. The pin is new, or it takes the place of one that existed.
  `{amount: {before: number | null, after: number}}`. `before` is `null` if no pin was there for
  this `(yearMonth, categoryId)`.
- `UNPIN` — a future pin is gone. Either `/pin` with `amount: null` cleared it, or a deactivation
  dropped it. `{amount: {before: number, after: null}}`.
- `CATEGORY_HARD_DELETE` — `{budgetRowsDeleted: number, transactionsDeleted: number, name: string}`.
  This holds counts and a snapshot, and not a before-and-after pair, because the category and its
  data are gone.

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
  does write one `UNPIN` entry for each pin it drops, as above. Those entries cover the changes to
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
  "rows": [{ "categoryId", "amount": number | null }, ...],
  "explanation": "...",
  "override": false
}
```

This endpoint replaces every row. The handler checks the request in this order:

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

`amount: number` → the app writes the row with that amount. `0` is valid, and it is a real target.
`amount: null` → the app writes the row with `amount=null`. That means "no target". The row stays
in the table, so the month stays dense.

Each difference makes one `CREATE` or `UPDATE` entry in the audit log, for one category.
`/replace` never makes a `DELETE` entry. The body rule stops a client from removing a row by
leaving it out, and a row whose amount becomes `null` is still a row. Such a row gives an `UPDATE`,
and not a `DELETE`.

### Pin write semantics

`POST /api/budget/{YYYY-MM}/pin` writes one or more explicit pins on a future editable year-month. Body:

```
{
  "rows": [{ "categoryId", "amount": number | null }, ...],
  "explanation": "...",
  "override": false
}
```

This endpoint writes a row, or changes the row that is there. It does not touch a category that the
body leaves out.

`amount: number` → the app writes a pin row at that amount, or changes the row that is there.
`amount: null` → the app **deletes** the pin row, if one exists. No row means no pin, and the
display then walks back to find a value.

For each category, the response holds a warning:

```
{
  "rows": [{ "categoryId", "amount", "stored": "PINNED" | "UNPINNED" }, ...],
  "warnings": {
    "downstreamPins": [
      { "categoryId", "yearMonth", "amount" }
    ],
    "pinMatchesCarriedValue": [
      { "categoryId", "amount" }
    ]
  }
}
```

- **downstreamPins** — for each category in the request, this lists the other pins for that same
  category in months later than the month the user changed, inside the editable range. The
  frontend shows them, so that the user can clear them in a second call. (Spec 19.)
- **pinMatchesCarriedValue** — this appears for each category where the new pin amount equals the
  value that the walk-back would give without the pin. The frontend then warns the user. The user
  is pinning the value that is there already, and that stops it from moving ahead later. (Spec 20.)

Each pin and each unpin makes a `PIN` or an `UNPIN` entry in the audit log.

## Carry-Forward

### Storage model: dense vs. sparse

The Budget table holds two kinds of row only: a row the user wrote, and a row the app made dense.

- **A locked month, a grace month, and the current month are always *dense*.** Each holds exactly
  one row for each category that existed when the app made the month dense, and any row the user
  wrote after that. A row may hold `amount=null`, which means "no target". For density, only the
  row itself counts.
- **A future month is *sparse*.** It holds only the rows the user pinned, through
  `POST /api/budget/{YYYY-MM}/pin`. A category without a pin has no row at all.

This split has three results, and each is worth a clear statement:

1. **A locked month never walks back.** The dense set of rows is the historical record. The app
   writes that set when it makes the month dense, either by the cron job at the month change, or
   later when it catches up. A change to an earlier locked month, through the admin override,
   cannot change what a later locked month shows.
2. **A change to a future month touches one row.** To set `December 2026 ABC = $2000` writes one
   row. `DEF` gets no row in December until the user pins it, or until December arrives and the app
   makes the month dense.
3. **The app resolves a future month one category at a time.** For each category that is active
   now: if a pin exists at `(future_ym, category)`, use it. If not, walk back one month at a time,
   with no fixed limit, until a row appears or the history ends. Then use the amount on the row it
   found. If it finds no row at all, the category has no target.

### Densification

To densify is to change the current year-month from sparse to dense. This happens in two ways:

**On a schedule.** EventBridge Scheduler starts a Lambda at `0 0 1 * ? *`, with
`ScheduleExpressionTimezone: "America/New_York"`. That is midnight ET on day 1 of each month. The
job is idempotent, so a second run does nothing.

**On demand, as a fallback.** The scheduled job may not have run for a year-month. It may have
failed, or the user may return after a long time away. The app then densifies during the next
request about the budget. Four requests start it: any `GET /api/budget/{yearMonth}`, whichever
month the caller asks for; any `GET /api/summary?month={yearMonth}`; and any budget write, through
`/replace`, `/pin`, or `POST /api/categories`. The handler always brings the table up to
`current_ym` before it answers. This holds even when the caller asks for a past or a future month.
The current month must be dense first, or the walk-back and the walk-forward give wrong results.
When this runs during a request, the request waits for it to finish.

### Walk-forward through missed months

The app first finds the year-months between `M_last` and `current_ym`. `M_last` is the most recent
month that is dense already. `current_ym` is the month that holds now, in ET. It then takes each
month `M` in that range, oldest first:

1. For each category that is active now:
   - If a row exists at `(M, categoryId)`, from a pin the user wrote earlier, leave it.
   - If not, walk back from `M - 1` to `M_last`, and find the most recent row for that
     `categoryId`. Copy its `amount`, which may be `null`.
   - If no earlier row exists anywhere, write a row with `amount=null`.
2. The app adds no inactive category. It leaves in place a pin on an inactive category that exists
   already at `M`. The user may make the category active again, or may want the history.

**The cold start, when no dense month exists.** `M_last` has no value when the Budget table is
empty, so there is nothing to walk back to. The app then densifies `current_ym` only: it writes a
row with `amount=0` for each category that is active. This is almost impossible in normal use. It
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
`dict[(yearMonth, categoryId), amount]`, and then does every walk-back in memory. This applies to
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
  month, until the user pins a different amount.
- **Deactivate** (`POST /api/categories/{id}/deactivate`) — this returns
  `affectedMonths: [{yearMonth, amount}]`. It lists every **future** month with a pin for this
  category. With `confirm: true`, the transaction deletes those future pins, and it writes one
  `UNPIN` audit entry for each pin it drops. The budget-target audit trail therefore stays
  complete — see **Audit scope**. The app leaves the past dense rows as they are, in the current
  month, the grace month, and every locked month. The history therefore stays true, as Spec 2
  requires.
- **Reactivate** (`POST /api/categories/{id}/reactivate`) — this changes the flag back. It does not
  restore a future pin that the deactivation dropped. The category joins the active set again. The
  next densify step, or any write to the current month, then treats it like a new category for
  that month.

### Edge cases

- **A new category, with no history.** The densify step finds no row to walk back to, so it writes
  `amount=null`. The category then appears in the current month with no target, and the user must
  set one.
- **A pin equal to the value that carries forward.** The app permits this, and it warns the user.
  See **Pin write semantics** and `pinMatchesCarriedValue`. The server writes the pin as asked.
- **A pin stays through the month change.** When a pinned future month becomes the current month,
  the densify step finds the pin already in place, and leaves it. The pin is then a normal dense
  row.
- **A pin on a category that the user deactivates before the future month arrives.** The
  deactivation deletes the pin, as above. The pin can survive if the user got past the confirm
  step, for example by a direct call to the API. The densify step then ignores it when that month
  arrives, because the app does not densify an inactive category. While the month stays editable,
  no summary shows the lost pin. The Spec 5 rule for an editable month shows only the active
  categories, and the categories with transactions. When that month locks, the Spec 5 rule for a
  locked month applies, and the lost budget row appears in the summary with `$0` of actuals. To
  remove it for ever, make the category active again and deactivate it in the normal way, or hard
  delete the category.
- **Two tabs that change the same thing.** The last write wins. There is no ETag and no version
  check. This is acceptable for one household.

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
  `UPDATE`, or `PIN` entry for that category. The app can no longer find the name, so the UI shows
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
| `POST /api/budget/{YYYY-MM}/pin` on a locked month | Pin controls hidden. | Pin controls visible. Save button reads "Save with override". | Yes — same `explanation` field, written to the audit log with `override=true`. |
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

## LLM-Driven CSV Import (`budget/csv_import.py`)

Each upload makes two calls to the Anthropic API. Both read the API key from SSM, at
`/budget/{stage}/anthropic-api-key`:

1. **Find the columns** — from the CSV headers and about 5 sample rows, find which column holds the
   date, the description, and the amount. Also find whether the amounts need their sign changed,
   because some statements use a negative number for a purchase.
2. **Give a category** — from every transaction description, and from the categories the user has
   active, give the best category to each row.

The upload endpoint returns the rows with their columns and categories. The user then reads them,
changes what is wrong, and commits.

### Upload response shape

`POST /api/transactions/upload` returns each row, and a note on each row about what is wrong with
it. The review page can then show every problem before the user clicks Commit:

```
{
  "rows": [
    {
      "transactionDate": "2024-03-15",
      "description": "...",
      "amount": 12340000,
      "categoryId": "01HQ...",         // null if LLM could not assign
      "validation": {
        "editability": "EDITABLE" | "GRACE" | "LOCKED",
        "issues": ["missing_category", "locked_month"]
      }
    },
    ...
  ]
}
```

The `issues` array holds two values, and no others:

- `missing_category` — the LLM gave no `categoryId`, or it gave one that matches no category. The
  user must choose one on the row.
- `locked_month` — the year-month of `transactionDate` is `LOCKED`, as the editability function
  says. The user must change the date, or turn on the admin override.

This endpoint sends no other issue string. A row that the app cannot read at all — a date in the
wrong form, or an amount that is not a number — makes the whole upload fail. The app then returns
one error at the top level, which names the bad rows.

The client blocks any row whose `issues` array is not empty. The user must correct it on the row,
by a choice of category or a change of date, or must turn on the admin override. The UI does not
permit Commit until every row is valid, or until the override is on.

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

**A retry is not idempotent.** Each request makes new ULIDs. So a retry after a `500` can write a
second copy of a row that the first attempt wrote before it failed. This is an accepted trade-off,
and it keeps the server simple. This failure is rare. A user who meets it must read the ledger and
remove the copies by hand, and may need the admin override for a locked month.

The older path, `POST /api/transactions`, stays. The UI uses it to add one transaction. It is not
the path for a CSV commit.

## Environment Variables

### Budget Handler Lambda

```
+-----------------------------+-----------------------------------+
| Variable                    | Purpose                           |
+-----------------------------+-----------------------------------+
| CATEGORY_TABLE_NAME         | DynamoDB Category table           |
| BUDGET_TABLE_NAME           | DynamoDB Budget table             |
| TRANSACTIONS_TABLE_NAME     | DynamoDB Transactions table       |
| BUDGET_AUDIT_LOG_TABLE_NAME | DynamoDB audit log table          |
| SSM_API_KEY_PARAM           | SSM path to the Anthropic API key |
+-----------------------------+-----------------------------------+
```

Every environment variable goes through `get_env_var()`. That function raises an error if the value
is absent or blank.

## Frontend (`budget/`)

React and TypeScript, built by Vite.

The pages are `BudgetPage`, `TransactionsPage`, `CategoriesPage`, `SummaryPage`, `UploadPage`,
`AuditLogPage`, `AdminToolsPage`, and `CallbackPage`.

The shared parts are `auth.ts`, which holds the PKCE flow and permits one token request at a time,
and `api.ts`, which holds the typed wrappers. The shared components are `MonthPicker`,
`LoadingOverlay`, `StatusMessage`, `DevBanner`, and `AdminBanner`. `AdminBanner` is the banner that
stays on the screen while admin mode is on.

The CDK stack sends the build output to S3 with a `BucketDeployment`. That step also writes a
`config.json` beside it, which holds the Cognito domain, the client ID, and the stage. One bundle
therefore works in every stage. The stage value is what lets the frontend build the
`budget-admin-{stage}` group name it expects.

### Pinned-row indicator

For any year-month later than the current one, the server adds a `pinned: bool` flag to each
summary row and each budget row. The frontend draws a pinned row differently, with a coloured dot
or a chip that reads "pinned". The user can then see at once which targets hold, and which ones
carry forward (Spec 17). When a pinned year-month becomes the current, grace, or locked month, the
server stops sending the flag. The row is then a normal dense row, and the app shows nothing about
the pin that made it.

When the user saves a pin, the response holds `warnings.downstreamPins` and
`warnings.pinMatchesCarriedValue`. The UI shows a box with these warnings, and two buttons: "Keep
pins" and "Clear downstream pins". It reports success only after the user chooses.

### Audit log page

`AuditLogPage` calls `GET /api/audit-log?limit=N`. The user sets `N` in the UI. The default is
`10`, and the server permits no more than `200`. The page shows the most recent entries, and it
shows the `override` flag on each one.

## Testing

The Python unit tests are in `lambda/tests/`. These files hold the budget tests:
`test_budget_handler.py`, `test_budget_categories.py`, `test_budget_budget.py`,
`test_budget_transactions.py`, `test_budget_summary.py`, `test_budget_audit.py`, and
`test_budget_csv_import.py`. Every DynamoDB call and every Anthropic call is a mock. Run
`uv run pytest` from `lambda/`.

The CDK tests are in `infra/test/`. Run `npm test` from `infra/`.

## Key Design Decisions

- **Two stacks** — The DynamoDB tables stay apart from the web stack. The data then survives a
  deploy of the app, and the prod removal policy can be `RETAIN` without a tie to the Lambda.
- **An amount is an integer in millionths** — Full precision, and no floating-point drift.
- **A ULID for each identifier** — It sorts by the time of creation, it hides nothing in a URL, and
  two writers need no agreement to make one.
- **A sparse future, and a dense present and past** — A pin is the only Budget row that exists for
  a future month. The densify step writes the dense rows at the month change, from the cron job or
  from the fallback. The future therefore stays live, and it follows a change in an earlier month.
  A locked month stays a complete record on its own, and it needs no walk-back when the app shows
  it.
- **`POST` for every change, except the hard delete** — `POST` for a budget write, through
  `/replace` or `/pin`, and for a transaction write. The body then survives CloudFront, and every
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
  budget this". It keeps every Budget row and every Transaction, it drops the future pins when the
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
  any row, so no row for a locked month gets in. But a failure part way through leaves the ledger
  with some rows written, and a retry by the client can write a second copy of a row. We chose this
  over the work to build a batch ID that makes a retry safe.
- **The audit log holds budget targets only** — The app does not audit a change to a transaction.
  The schema stays simple, and it matches how the user reviews the data. What matters is that
  *budget against actual* is correct for a past month, and not a record of who edited which
  transaction.
- **A history of each category name** — A rename is always permitted, and it adds to the
  `nameHistory` list on the category row. A locked month can then find the name that was in use at
  the time. No Budget row and no Transaction row needs a copy of the name.
