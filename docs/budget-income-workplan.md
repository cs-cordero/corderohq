# Workplan - Budget Income and Left to Assign

This is the plan to build Specs 31 to 57 (`docs/budget-specs.md`), as the design describes
(`docs/budget-income-design.md`). Read both documents first. Read **Decisions and Reasons** in the
design before you change any decision.

## How to Use This Plan

- Do the checkpoints in order. Each checkpoint leaves the app in a working state, and the user can
  deploy it alone.
- At the start of a session, find the first checkpoint with a box that is not checked. Read its
  **Before you start** list. Then continue from the first box that is not checked.
- Check each box (`- [x]`) in the same commit as the work that completes it.
- Add one line to the **Progress Log** at the end of each session.
- If the code is different from what the design says, stop and tell the user. Do not guess. Then
  correct the design in the same commit as the code.

## Rules for Each Session

These come from `CLAUDE.md` and the user. Obey them.

- Write all prose in ASD-STE100: replies, comments, docstrings, docs, and commit messages.
- A commit message has one line and no body. It has no "Co-Authored-By" line.
- **Do not run a test, a build, or a lint command.** Tell the user which command to run, and what
  to look for. The user sends back the output that matters. The commands for each checkpoint are
  in its **Checks** list.
- Give a type to everything in Python. A private constant at module level has a name in ALL_CAPS
  with one underscore in front, such as `_INCOME_PLAN_TABLE`.
- Read each environment variable with `get_env_var()` from `util.py`.
- When a CDK stack gets a new resource, change the JSDoc of the stack class to list it.
- Use no deprecated API or package.
- Do not put spec numbers ("Spec 45") in code comments, docstrings, or test docstrings. Say the
  rule in plain words. Spec numbers are fine in the docs.
- The repository is public. Put no real names, employers, amounts, or account numbers in the code,
  the tests, or the docs. Use made-up values such as "Employer A".

## Commands

Give the user these commands. Each one runs from the repository root.

| Name | Command |
|---|---|
| All checks, read-only | `./check.sh -n` (`-p` for Python only, `-t` for TypeScript only; without `-n` it fixes files in place) |
| Python tests | `cd lambda && uv run pytest` |
| One Python test file | `cd lambda && uv run pytest tests/<file>.py` |
| Python lint and format | `cd lambda && uv run ruff check . && uv run ruff format --check .` |
| Python types | `cd lambda && uv run ty check` |
| CDK build, lint, and tests | `cd infra && npm run build && npm run lint && npm test` |
| Frontend build and lint | `cd web/personal-finance && npm run build && npm run lint && npm run format:check` |
| Deploy | See **Deploy** in `docs/user_guide.md`. Deploy to dev first. |

---

## CP0 — Check Before You Start

No code. The goal is to confirm that the design still matches the code.

- [x] Read `docs/budget-specs.md` (Definitions, Specs 31 to 57, Deferred).
- [x] Read `docs/budget-income-design.md` in full.
- [x] Open each file in the **Code Map** of the design, and confirm that the names in it exist:
      `_ensure_hydrated()`, `_validate_commit_rows()`, `_post_transactions_upload()`,
      `_post_transactions_commit()`, `_put_transaction()`, `_put_account()`, `compute_summary()`,
      `TransactionsTable.update()`, `densify()`, `resolve_future_targets()`, `map_columns()`,
      `categorize_transactions()`, `AccountType`, `_ACCOUNT_TYPE_META`, `ACCOUNT_TYPES`,
      `BUDGET_LINKS`.
- [x] Ask the user about the open item "the `credit_card` account type" in the design. Record the
      answer in the design.
- [x] Tell the user about each difference that you found. Correct the design.

---

## CP1 — Dispositions and the Note (Specs 45 to 51)

The user can mark work travel as Off-budget, and the summary stops the count of Skip rows. No
income work yet.

**Before you start:** design sections **Changed table: `Transactions-{stage}`**, **Summary**, and
**Transaction API**.

Backend:

- [x] `budget/dispositions.py` (new): `disposition_of(txn) -> str`, where an absent value returns
      `"BUDGET"`; the three values as constants; and `resolve_disposition_fields()`, which checks
      the rules. The handler and `aws/dynamodb.py` both use it.
- [x] `TransactionsTable.create()` and `batch_create()`: store `disposition`, `accountId`, and
      `note` when they are present.
- [x] `TransactionsTable.update()`: `SET` the new fields. `REMOVE` `categoryId` for a change to
      SKIP. `REMOVE` `note` for an empty note.
- [x] Handler `_post_transaction()`, `_put_transaction()`, `_post_transactions_commit()`: accept and
      validate `disposition` and `note` (design **Transaction API**). `categoryId` is required only
      for BUDGET.
- [x] `_validate_commit_rows()`: `missing_category` only for a BUDGET row, or an OFF_BUDGET row
      with an unknown `categoryId`.
- [x] `compute_summary()`: only BUDGET rows count in a category and in the membership rules. Add
      `offBudget` and `totalsWithOffBudget` to the result.
- [x] `_post_budget_replace()`: the body rule ("every category with a non-zero transaction") reads
      BUDGET rows only. A SKIP row has no `categoryId`, so the old code failed on it.

Frontend:

- [x] `types.ts` and `parsers.ts`: `Transaction` gets `disposition` (the parser gives BUDGET when
      it is absent), and optional `categoryId`, `accountId`, and `note`. `categoryId` was
      required before, so a SKIP row would have broken the parser. `Summary` gets `offBudget` and `totalsWithOffBudget`. The parser must accept a row
      with no `disposition`.
- [x] `api.ts`: `createTransaction()` and `updateTransaction()` send `disposition` and `note`.
- [x] `TransactionsPage.tsx`: disposition and note columns. A "Show skipped rows" filter, off by
      default. The edit of a row can change the disposition and the note. Hide the category picker
      for SKIP.
- [x] `SummaryPage.tsx`: the Off-budget row and the "TOTAL + off-budget" row below the TOTAL row.
- [x] `UploadPage.tsx`: no change yet. Its rows commit as BUDGET, the same as today.

Tests:

- [x] `test_budget_dispositions.py` (new): the disposition rules and the note rules.
- [x] `test_budget_transactions.py`: `create()`, `batch_create()`, and the `REMOVE` cases of
      `update()`.
- [x] `test_budget_summary.py`: an OFF_BUDGET row and a SKIP row do not change a category; the
      membership rules; `offBudget`; `totalsWithOffBudget`; a row with no `disposition` counts as
      BUDGET.
- [x] `test_personal_finance_handler.py`: the new body fields on the three routes, and the
      `/replace` body rule, which now ignores the categories of OFF_BUDGET and SKIP rows.

Checks (the user runs them):

- [x] Python tests, lint, format, and types pass.
- [x] Frontend build and lint pass.
- [x] Dev deploy. Mark one transaction Off-budget with a note, and one transaction Skip. The
      summary shows the expected totals.

---

## CP2 — Make the Densify Code Generic (no change in behavior)

A refactor only. The budget must work exactly as before.

**Before you start:** design section **Income Plan Month Rules**. Read `budget/densify.py` and
`tests/test_budget_densify.py` in full.

- [x] Add the `DenseSparseStore` protocol in `budget/densify.py`. The module no longer imports
      `aws/dynamodb.py`.
- [x] Change `densify()` and `resolve_future_targets()` to take the protocol and a list of active
      keys. Keep the cold-start value as a parameter (`0` for the budget). Add `resolve_future()`
      (generic) and `value_at_or_before()`.
- [x] `BudgetCarryStore` (an adapter in `aws/dynamodb.py`) implements the protocol for the Budget
      table. `active_category_ids()` gives the active keys.
- [x] Change every caller: `_ensure_hydrated()`, `_get_budget()`, the pin code, and
      `scheduled_densify.py`.
- [x] `compute_summary()` uses `value_at_or_before()`. Delete its copy of the walk-back code.
- [x] The existing tests in `test_budget_densify.py` change only how they call the functions.
      New tests drive the generic functions with an in-memory store.

Checks:

- [ ] Python tests, lint, format, and types pass.
- [ ] Dev deploy. The budget page and the summary show the same values as before.

---

## CP3 — The Income Plan: Backend (Specs 31 to 37, 39)

**Before you start:** design sections **New table: `IncomeSource-{stage}`**, **New table:
`IncomePlan-{stage}`**, **Changed table: `BudgetAuditLog-{stage}`**, **Income Plan Month Rules**,
**Income Plan API**, and **Infrastructure**.

Infrastructure:

- [x] `personal-finance-dynamodb-stack.ts`: the two tables, two `readonly` properties, and the
      JSDoc.
- [x] `infra/app.ts`: pass the two tables to `PersonalFinanceWebStack`.
- [x] `personal-finance-web-stack.ts`: the props, the environment variables, and the grants for
      the API handler and for `densifyFn`. **The 12 new routes in the API Gateway route list**
      (the design missed this). The JSDoc.
- [x] `infra/test/`: the tables, the environment variables, and the grants.

Backend:

- [x] `aws/dynamodb.py`: `IncomeSourceTable` (list, get, create, update, deactivate, reactivate,
      reorder) and `IncomePlanTable` (the `DenseSparseStore` protocol for `SRC#` rows, and the
      one-time line methods).
- [x] A paycheck model with validation and the net pay calculation (design **New table:
      `IncomePlan-{stage}`**). Put it in a new module, `budget/income.py`.
- [x] `compute_income()` (design **Left to Assign**).
- [x] `_ensure_hydrated()` and `scheduled_densify.py` densify the income plan too.
- [x] The income routes in the handler (design **Income Plan API**), with the editability checks
      and the override.
- [x] The audit actions and their `changes` payloads. `AuditLogTable.write_entry()` takes
      `category_id=None`, `income_source_id`, and `income_line_id`.
- [x] Frontend, moved here from CP4 so that the audit page does not break in dev:
      `AuditEntry.categoryId` is optional in `types.ts` and `parsers.ts`, and `AuditLogPage`
      shows "Income" for an entry with no category.

Tests:

- [x] `test_budget_income.py` (new), which also holds the income plan densify tests: the paycheck validation and the net pay; the income source
      life cycle; `/replace`; `/pin` with both warnings; the one-time routes; the audit entries;
      the editability checks; `compute_income()` for an editable, a future, and a locked month.
- [x] The income plan walk-forward, and a one-time line that never carries forward (in
      `test_budget_income.py`, with the `IncomePlanCarryStore` tests).

Checks:

- [x] Python tests, lint, format, and types pass. CDK build, lint, and tests pass.
- [ ] Dev deploy. Call the new routes with a made-up income source. The audit log shows the
      entries.

---

## CP4 — The Income Plan: Frontend (Spec 38)

**Before you start:** design section **Frontend**. Read `BudgetPage.tsx` for the edit, pin, and
explanation flow to copy.

- [x] `types.ts`, `parsers.ts`, `api.ts`: the income source, the paycheck, the one-time line, and
      the `GET /api/income/{YYYY-MM}` response.
- [x] `App.tsx`: the `income` route. `Layout.tsx`: an "Income" link in `BUDGET_LINKS`.
- [x] `IncomePage.tsx` (new): the month picker, the paycheck breakdown for each source, the
      one-time lines, the planned net income, the edit flow (`/replace` or `/pin`, the pin chip,
      the downstream-pin warnings, the explanation), and the income source management.
- [x] `AuditLogPage.tsx` and `AuditEntry` in `types.ts`: the new actions. `categoryId` becomes
      optional (done in CP3). The page shows the label of the income source or the one-time
      line, and a paycheck as its monthly net pay.

Checks:

- [ ] Frontend build and lint pass.
- [ ] Dev deploy. Add an income source with deductions. Pin a future month. Add a one-time line.
      Lock a month with the simulated date, and confirm that it keeps its values.

---

## CP5 — Left to Assign (Specs 40 to 43)

This checkpoint delivers the main goal.

**Before you start:** design section **Left to Assign**.

- [x] `_get_summary()`: add the `income` object from `compute_income()` and
      `totals.budgeted`.
- [x] `compute_summary()` reads the current month in ET, not in UTC (the old bug).
- [x] A shared `components/LeftToAssign.tsx` draws the line on both pages.
- [x] `types.ts` and `parsers.ts`: `Summary.income`.
- [x] `SummaryPage.tsx`: the header line "Planned net income · Left to assign". A different colour
      when the value is not `$0`.
- [x] `BudgetPage.tsx`: fetch `GET /api/income/{YYYY-MM}`. Show the same header line. Calculate
      the value live from the form. It adds the active categories only, the same as the summary
      for an editable month. (A deactivated category with a row and a transaction in the month is
      in the summary total but not in the live total. That case is rare.)
- [x] Tests: `leftToAssign` in the summary route (`test_personal_finance_handler.py`); the ET
      month in `compute_summary()` (`test_budget_summary.py`). The membership of each half is
      already tested by the summary tests and the `compute_income()` tests.

Checks:

- [ ] Python tests and frontend build pass.
- [ ] Dev deploy. Change a target, and watch the value change before the save.

---

## CP6 — Accounts, the Credit Card Type, and the Debt Class (Spec 44)

**Before you start:** design sections **Changed table: `Account-{stage}`** and **Accounts**.

- [x] `networth/models.py`: the `credit_card` type, the `debt` class, and the meta. Change
      `mortgage` and `other_liability` to the fixed class `debt`. Reject `debt` for a type that
      lets the user choose classes.
- [x] A `budget_role_of(account)` function with the default table from the design.
- [x] `AccountTable.update()` and `_put_account()`: `budgetRole` and `lastFour` are editable.
      Validate `lastFour` (each value is exactly 4 digits).
- [x] `types.ts`: `credit_card` in `ACCOUNT_TYPES`, `ACCOUNT_TYPE_META`, and `ACCOUNT_TYPE_LABELS`.
      `debt` in `ASSET_CLASSES` and `ASSET_CLASS_LABELS`. `mortgage` and `other_liability` use
      `debt`. `Account` gets `budgetRole` and `lastFour`.
- [x] `AccountsPage.tsx` and `NetWorthEntryPage.tsx`: leave `debt` out of the class pickers.
      (`debt` has a label in `ASSET_CLASS_LABELS`, but it is not in `ASSET_CLASSES`, which only the
      pickers read.) `AccountsPage.tsx`: the budget role picker and the "last four digits" field.
      The create route takes no budget fields, so the page sends them in a second call.
- [x] `lambda/scripts/migrate_liabilities_to_debt.py` (design: the migration script). Check
      `import_networth_csv.py` too.
- [x] `test_networth_models.py` and the account tests: the new type and class, the default roles,
      the `lastFour` validation, and the rule that an asset account cannot choose `debt`.

Checks:

- [ ] Python tests and frontend build pass.
- [ ] The user runs the migration script on dev as a dry run, then with `--apply`, and gives the
      ID of the card account with `--to-credit-card`.
- [ ] Dev deploy. The card shows as "Credit Card". The net worth history of the card and the
      mortgage shows the same totals as before.
- [ ] The user deploys to prod, and then runs the script on prod.

---

## CP7 — CSV Import, Part 1: Account, Fingerprints, Dispositions (Specs 44, 46, 54, 56)

**Before you start:** design sections **Step 4: import fingerprints**, **Upload response**, and
**Commit**.

- [x] `_post_transactions_upload()`: require `accountId`, with a budget role. Return `accountId`.
- [x] Fingerprints: the function, the `n` count, and the `duplicate` flag from the saved rows.
- [x] For now, the suggested disposition is BUDGET for each row, and `suggestedBy` is `LLM`.
      CP9 replaces this.
- [x] `_post_transactions_commit()`: store the new fields. Drop the rows with a fingerprint that
      exists, and return them as `skippedDuplicates` (indices into the committed batch).
- [x] The review screen already has the CP9 parts that it can show: the `review` reasons, the
      "Look at these first" list, and the source of each suggestion (as a tooltip on the type).
      CP9 fills them in on the server.
- [x] `UploadPage.tsx`: the account picker (required). The disposition picker, the category
      picker, and the note on each row. Duplicate rows unselected, with a chip. Skip and
      Off-budget rows in a separate list.
- [x] `types.ts` and `parsers.ts`: `ParsedTransaction`, `RowValidation`, and the commit result.
- [x] Tests: the fingerprint (same file, two equal rows; a second upload of the same file), and
      the commit that drops duplicates.

Checks:

- [ ] Python tests and frontend build pass.
- [ ] Dev deploy. Upload a made-up CSV two times. The second upload marks every row as a
      duplicate.

---

## CP8 — CSV Import, Part 2: Less Data to the LLM (Spec 57)

**Before you start:** design sections **Step 2**, **Step 3**, and **Step 6**.

- [x] Local column types (`date`, `number`, `text`).
- [x] `map_columns()`: send the headers and the types only. Ask for `bankCategory` too.
- [x] The local sign check, and a "flip all signs" button on `UploadPage.tsx`. The button sends
      the file again with `flipSigns: true`, because the fingerprint of a row holds its amount.
      The response has `signCheck: {llmInverted, locallyFlipped, userFlipped}`.
- [x] The bank category hint, with the generic word list. The upload keeps it on each row as
      `_bankHint` for CP9, and removes it before the response.
- [x] `categorize_transactions()`: unique descriptions only; return a `kind` as well. The upload
      keeps it as `_kind` for CP9. The LLM call now runs even with no active categories, because
      the kind is useful without them.
- [x] Tests: no cell value is in the prompt (assert on the text that goes to the mock client);
      each description appears one time; the `kind` parse; the hint word list.

Checks:

- [ ] Python tests pass.
- [ ] **The user tests the column mapping with real CSVs from each bank, in dev.** Record the
      result in the Progress Log. If the sign is often wrong, stop and ask the user.

---

## CP9 — CSV Import, Part 3: The Other Side and the Direction Table (Specs 52, 53, 55)

**Before you start:** design sections **Step 5** and **Step 7**.

- [x] The pair match: the saved rows of the year-months in range (each upload month, and the
      month before and after it). **Not the same upload:** every row of one upload belongs to the
      same account, so the other side of a transfer can never be in it. The design said so by
      mistake.
- [x] The logic is a pure module, `budget/directions.py`, with no table access.
- [x] The last-four match.
- [x] The direction table, and the `review` reasons. New reason: `unknown_direction`, for a row
      where no step finds the other side. An account with no budget role (a mortgage, a loan) is
      outside the budget, so a payment to it is an expense.
- [x] `UploadPage.tsx`: the rows marked for review first, with the reason. The source of each
      suggestion.
- [x] Tests (`test_budget_directions.py`): each row of the direction table; the pair match with
      none, one, and many candidates; the last-four match.

Checks:

- [ ] Python tests and frontend build pass.
- [ ] Dev deploy. Upload a made-up checking CSV and a made-up savings CSV with one transfer
      between them. The transfer counts one time.

---

## CP10 — Documentation

- [ ] Move the final design into `docs/personal-finance-architecture.md`: the data model, the
      routes, the month rules for income, the audit actions, the CSV import, the environment
      variables, and the frontend pages.
- [ ] `docs/user_guide.md`: how to set up income sources, accounts, and dispositions.
- [ ] Delete `docs/budget-income-design.md` and this workplan.

---

## Progress Log

One line for each session: the date, the checkpoint, and what changed or what is blocked.

- 2026-10-17: Specs, design, and workplan written. No code yet.
- 2026-10-17: CP0 done. The user chose a `credit_card` type and a `debt` class for all liabilities,
  with a migration of the existing data (CP6).
- 2026-10-17: CP1 code and tests written and committed. The CP1 checks are not run yet.
- 2026-10-17: CP1 Python checks pass, after three fixes for older type errors in the net worth
  code. The frontend checks and the dev test are still open.
- 2026-10-17: CP1 complete. The user confirmed that all checks pass. Next: CP2.
- 2026-10-17: CP2 code written. The checks are not run yet.
- 2026-10-17: CP3 code and tests written. The checks are not run yet. Found: the route list in
  API Gateway, and an old bug (`compute_summary()` reads the current month in UTC, not ET).
- 2026-10-17: CP3 checks are clean (`./check.sh -n`). The dev test is still open.
- 2026-10-17: CP4 code written. The user asked for one big check after many CPs.
- 2026-10-17: CP5 code written.
- 2026-10-17: CP6 code written. The migration script is not run yet.
- 2026-10-17: CP7 code written.
- 2026-10-17: CP8 code written. Found an older problem: the upload stores the date text of the
  bank as it is, so a format such as `03/15/2026` gives a wrong year-month. Not fixed.
- 2026-10-17: CP9 code written.
