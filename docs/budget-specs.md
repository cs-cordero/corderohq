# Specs - Family Budget App

## Definitions

- **Current year-month** — the year-month that holds "now" in US/Eastern. The budget always uses
  ET, wherever the user is.
- **N** — the number of future year-months that the user can see and can budget, counted from the
  current year-month.
- **Grace period** — a period after a year-month ends, in which the year-month stays editable. At
  the end of the grace period, the year-month becomes **locked**.
- **Editable year-month** — the current year-month, the previous year-month while its grace period
  continues, or any of the next N year-months.
- **Locked year-month** — any year-month that is not editable.
- **Override** — a way for the user to act on a locked year-month. The server must enforce the
  override. It is not enough to hide the action in the UI.
- **Income source** — one stream of income that the plan counts, such as a job, interest, or
  dividends. Each income source has one or more owners, the same as an account: a joint source,
  such as the interest of a joint account, has two. A person can own any number of income sources.
  Every kind of income source works the same way.
- **Payment** — the gross amount of one payment from an income source, such as one paycheck, and
  the list of deductions from it. A payment with no deductions, such as interest, is also valid.
- **Net amount** — the gross amount of a payment minus all its deductions.
- **Planned net income** — for one year-month, the net amount of each active income source times its
  payments per month, plus every one-time income line in that year-month.
- **Left to assign** — for one year-month, the planned net income minus the sum of all budget
  targets. The goal is `$0`.
- **Disposition** — the effect of a transaction on the budget: **Budget**, **Off-budget**, or
  **Skip** (Spec 45).
- **Spending account** — an account that pays for expenses, such as a checking account or a credit
  card.
- **Holding account** — an account that keeps money, such as a savings fund, a brokerage account, or
  a 529 account.

## Budget Summary

1. I want a page with a table that summarizes the current budget. Spec 5 defines which categories
   the table shows. Each row shows the category, its budgeted amount for the year-month, and the
   actual expenditures for the year-month.
2. I want to see any previous year-month as it was at the end of its grace period. A locked
   year-month must look the same in 20 years as it looked on the day it locked. A budget change
   made later through the override is an exception, and the audit log must show it. The app does
   not audit a transaction change made through the override — see Spec 9 for that trade-off.
3. I want to see the next N year-months. The actual expenditures are usually empty for those
   months, but the budgeted amounts must be visible.
4. The next month becomes the current year-month when the US/Eastern clock goes past midnight on
   the first day. The previous year-month stays editable for a grace period of 7 days. It then
   locks. After that, the app rejects a change to its budget targets or to its transactions,
   unless I use the override.
5. The categories on the summary table are the superset of these:
   - For an editable year-month: the categories that are active now, and any category with
     expenditures other than zero in that month.
   - For a locked year-month: every category that had a budget target, or any expenditures in that
     month. "Had a target" means a recorded amount, and `$0` counts; `null` means no target. This
     applies even if the category is not active today. A deactivated category stays on a past
     month, so the historical view stays true.

## Transaction Ledger

6. I want to report transactions to the ledger. The app adds these together to make the actual
   expenditure values on the Budget Summary page.
7. I want these columns in the ledger: transaction date, description, budget category, amount.
8. The app must reject an insert, an update, or a delete of a transaction whose date falls in a
   locked year-month, unless I use the override. A transaction with a future date is permitted
   only if its date falls in an editable year-month — the current one, the next N, or the previous
   one in grace.
9. I want to upload a CSV of transactions. The app must use a low-cost LLM to map the columns, and
   to give a category to each row from my active categories. After the upload, I want an editable
   table of the parsed rows and their categories, with a Commit button. The review screen must show
   each broken rule beside its row — for example, a row dated in a locked month, or a category that
   is absent or unclear. The app must not wait until I click Commit to report them. The app must
   block Commit completely while any row is invalid, until I correct the rows or use the override.

## Budget Category

10. I want a page that shows all active budget categories, and a toggle to show the inactive ones
    as well.
11. I want to change the budgeted amount for each category in any editable year-month. The app must
    reject a change to the amount in a locked year-month, unless I use the override.
12. I can set a budget target only for an editable year-month — the previous one in grace, the
    current one, or any of the next N — unless I use the override.
13. I can rename a category at any time, whatever the lock state of any year-month. When a renamed
    category appears on a locked year-month, the app must show the rename — for example,
    `Groceries (renamed to: Food)`. I can then match what I see against a paper record from that
    time.
14. When I make a category, I must put it in a group (Spec 23). The first budget target is `$0` by
    default. That is a valid target, and it means "no spend planned this month". I can give a
    different amount instead, but it must not be negative. If the category is active, it must
    appear at once in the summary for the current year-month, with its first target.
15. Deactivation is the normal way to retire a category. It must be common and easy. It keeps every
    Budget row and every Transaction in the current, grace, and locked year-months, so the
    historical record stays whole (Spec 2). The app drops any future pinned amount for the category
    (Spec 18) when I confirm, as Spec 21 describes.
16. A hard delete is a different operation, and it destroys data. It removes the category for ever.
    It also deletes every Budget row and every Transaction that points at the category, in all
    year-months, including the locked ones. It breaks the historical fidelity of Spec 2 for that
    category on purpose. That is its point. Therefore:
    - Only an admin can do it. It stays out of the normal categories UI, and only an explicit admin
      tools page reaches it.
    - It needs a typed confirmation, such as the category name, and an explanation that the app
      records.
    - It is the only category operation that the audit log records, because no one can undo it.
    - Use deactivation for "I no longer want to budget this". Keep the hard delete for "this
      category should never have existed".

## Category Groups and Ordering

23. I want to put my categories into named **groups**, such as "Income", "Essentials", and
    "Discretionary". Every category belongs to exactly one group. No category is without a group.
24. Every category and every group has an explicit display order. On the Budget Summary and in the
    budget editor, the app sorts the categories first by the order of their group, and then by
    their position in that group. Each group shows as a section, with the group name in a header
    row above its categories.
25. I want to change the order of the groups, and the order of the categories in a group, by drag
    and drop on the Categories page. I also want to drag a category from one group to another. The
    app must keep the order and the moves between sessions.
26. I want to make a new group at any time. Each group name must be unique, and case does not
    count. The app puts a new group at the end of the order. I can then move it.
27. I can rename a group at any time. The new name shows everywhere the app draws the group name.
    Unlike a category rename (Spec 13), a group rename keeps no historical name. A group is
    organizational data only. It is not the historical record of where a transaction was budgeted.
28. I can delete a group when no **active** category points at it. On delete, the app moves any
    deactivated category in that group to the **Unassigned** group (Spec 30), so no category is
    left without a group. A group has no deactivation step. A group is organizational data only,
    and no transaction or budget row points at one, so the rule that protects a category does not
    apply. To delete a group is one click and one confirmation, and the group is then gone.
29. The app makes the "General" group on the first deploy. It is the home for any category that
    existed before. After that, it acts like any other group. I can rename it, move it, or delete
    it once I move its active categories somewhere else.
30. A permanent **Unassigned** group exists, and the app marks it as a system group. No one can
    rename it, delete it, or move it, and it always sorts last. It has one purpose: to hold a
    deactivated category whose group was deleted. The rule in Spec 23, that every category belongs
    to a group, therefore holds at all times. The frontend hides the Rename and Delete buttons for
    it, and shows a short note in their place.

## Carry-Forward and Future Pinning

17. When I look at a future year-month, the app derives the budgeted amounts. For each category
    that is active now, it shows the value I set most recently in any earlier year-month. A value
    that I **pin** for that category in that future year-month takes the place of the derived one.
18. I want to pin the budgeted amount of a category for any future editable year-month. A pinned
    amount must:
    - Stay in place when that year-month becomes the current year-month. The carry-forward must not
      write over it.
    - Stop an earlier change from moving past the pinned year-month. For example, I pin December at
      $2000, and I later change October to $1500. November then takes the $1500, but December stays
      at $2000.
    - Look different on the budget summary, such as with a coloured marker. I can then see at once
      which rows are pinned, and which rows carry forward.
19. When a change of mine conflicts with a pin further ahead, the app must warn me. It must show me
    which pins exist, and let me keep them or clear them.
20. When I pin a future year-month to the same value that the carry-forward would give, the app
    must warn me. I am then pinning the value that is there already, and that stops it from moving
    ahead later.
21. When I try to deactivate a category with future pinned amounts, the app must warn me and list
    them. If I confirm, the app drops the pins. The category then leaves nothing behind in a
    future year-month.

58. When I change a budget target or an income payment in an editable month, I choose how the
    change applies:
    - **From this month on** — later months carry the new value forward, until a later change.
      In a future month, this is a pin (Spec 18).
    - **This month only** — only this month uses the new value. The next month goes back to the
      value that it would have had without the change. A this-month-only value never carries
      forward, and it does not stop an earlier change from moving forward.

    The app shows which kind of change each month has: "changes here" for a change from this
    month on, and "this month only". I can undo either kind. The audit log records which kind each
    change was. A one-time income line (Spec 35) is still the tool for extra money that is not
    part of a payment.

## Audit Log

22. I want a page that shows the most recent audit log entries for budget targets. The page must
    let me choose how many entries to get, and the default is the last 10. A hard delete of a
    category also appears in this log (Spec 16).

## Income Plan

31. I want to record each income source. An income source has one or more owners (people), a
    label, and a pay frequency, such as every two weeks. A person can own any number of income
    sources. I cannot delete a person who still owns an income source. The code
    must not contain an employer name or any other real income data.
32. For each income source, I want to record one payment: the gross amount, and a list of
    deductions. Each deduction has a label, a kind, and an amount for one payment. The kinds are: tax, pre-tax
    savings (such as a 401k or an HSA), pre-tax benefit (such as medical insurance or a DCFSA), and
    post-tax savings (such as a Roth 401k). The app calculates the net amount.
33. Each income source has a number of **payments per month** for the plan. The pay frequency sets
    the default: weekly 4, every two weeks 2, twice a month 2, and monthly 1. I can change it. The
    app uses the same number in every month. It does not add the
    third paycheck in a month that has three paychecks. I accept that the plan is then lower than
    the real income.
34. The income plan obeys the same month rules as a budget target. These are the grace period and
    the lock (Spec 4), the editable year-months (Spec 12), the override, the carry-forward (Spec
    17), and the pins (Specs 18 to 20). A change to a payment in one year-month carries forward to
    later year-months. A locked year-month keeps the payment that it had (Spec 2).
35. I want to add a **one-time income line** to an editable year-month. It has a label and a net
    amount. It applies to that year-month only, and it never carries forward. I use it for a bonus,
    an RSU payout, a DCFSA payout, a third paycheck, or any other extra money that I want to
    budget. Extra money without a one-time income line is not part of the plan.
36. The app does not model a change in the net amount during the year. Examples are a 401k deduction that
    stops at the annual limit, or a Social Security tax that stops at the wage limit. I change the
    payment myself when I want to.
37. I can deactivate an income source. The rules are the same as for a category (Spec 15). It keeps
    its history in the current, grace, and locked year-months. It drops its future pins when I
    confirm.
38. I want an **Income** page. It shows each income source, and the breakdown from the gross amount
    to the net amount for a year-month that I choose. It also shows the one-time income lines of that
    year-month. I make every change to the income plan on this page.
39. The audit log records each change to a payment, a pin or an unpin of a payment, and each
    change to a one-time income line. Each change needs an explanation, the same as a change to a
    budget target.

## Left to Assign

40. I want to see the **left to assign** amount for a year-month. The budget editor, the Budget
    Summary, and every other page that shows a top line must show it. These pages show the
    planned net income and the left to assign amount only. They do not show the breakdown from
    the gross amount to the net amount.
41. A positive left to assign amount is money with no job. A negative amount means that the
    targets are more than the planned net income. The app shows the amount, and it must make a
    value other than `$0` easy to see. It does not block a save.
42. A locked year-month shows the left to assign amount from its own income plan and its own
    targets, as they were when the year-month locked (Spec 2).
43. The app does not record actual income against the plan. A paycheck deposit does not change the
    left to assign amount.

## Accounts

44. Each CSV upload belongs to one account. I choose the account when I upload. The app uses the
    accounts of the net worth record. Each account has a **cash flow role**: spending, holding,
    external, or non-cash. An account that I use for an upload is spending or holding. Each
    transaction from an upload records its account.

## Transaction Dispositions

45. Each transaction has a disposition:
    - **Budget** — a normal expense or refund. It needs a category. It counts in the actual
      expenditure of its category. This is the default. Every transaction that exists today is a
      Budget transaction.
    - **Off-budget** — money that the budget must not count. Examples are work travel that my
      employer pays back, and a payment with money that already had its job, such as money from a
      savings fund or from the DCFSA. A category is optional. It does not count in the actual
      expenditure of any category.
    - **Skip** — money that is not an expense. Examples are a paycheck, a bonus, interest, a
      transfer between my own accounts, a credit card payment, and the payback from my employer. It
      has no category. It counts nowhere.
46. The app saves every transaction with its disposition, including a Skip transaction.
47. I can add an optional short note to any transaction, such as "work trip" or "vacation fund".
48. I can change the disposition of a transaction on the CSV review screen, and later in the
    ledger. The lock rules of Spec 8 apply to the change.
49. The ledger shows the disposition and the note of each transaction. It hides Skip transactions
    by default, and a filter shows them.
50. On the Budget Summary, only Budget transactions count as expenditures. This also applies to
    the category rules of Spec 5.
51. The Budget Summary shows an **Off-budget** row below the TOTAL line. Its budgeted amount is
    always `$0`. Its actual amount is the sum of the Off-budget transactions in the year-month. The
    TOTAL line does not include it. A second line below it, **TOTAL + off-budget**, shows the TOTAL
    actual plus the Off-budget actual.

## CSV Import: Dispositions and Data Sent to the LLM

52. The CSV import suggests a disposition for each row, from the direction of the money and the
    cash flow roles of the accounts:

    | From → to | Example | Suggested disposition |
    |---|---|---|
    | outside → spending | a paycheck, interest | Skip |
    | outside → spending | a refund | Budget, in the category of the purchase |
    | spending → outside | groceries, a hotel on a card | Budget |
    | spending → spending | checking pays the credit card | Skip |
    | spending → holding | checking to a vacation fund, a 529, a brokerage | Budget, in the savings category |
    | holding → spending | a vacation fund to checking | Skip |
    | holding → outside | the vacation fund pays a hotel | Off-budget |
    | holding → holding | savings to a brokerage | Skip |
    | outside → holding | an RSU sale, interest, dividends | Skip |

    Thus a transfer between two of my accounts counts one time at most, on the side where the money
    leaves a spending account.

    When the import cannot find the direction of a row with confidence, it suggests Skip, and it
    marks the row for my review. A missed savings transfer is easy to see on the summary. A double
    count is not easy to see.
53. A credit card payment must be Skip, in the checking CSV and in the card CSV. If not, the app
    counts the purchases two times.
54. The review screen shows Skip rows and Off-budget rows in a separate list. I can change the
    disposition and the category of any row before I commit.
55. The review screen marks a row that looks like the other side of a transfer. The other side has
    the same amount, the opposite direction, a date within a few days, and a different account. It
    can be in the same upload or in a transaction that I committed before. I confirm or reject each
    match.
56. The review screen marks a row that I committed before, such as from an earlier CSV with dates
    that overlap. The app does not commit that row unless I select it. A retry of a commit must
    not make a second copy of a row.
57. The CSV import must send the minimum data to the LLM:
    - To map the columns, it sends the header row and a type for each column, such as date,
      number, or text. The code finds the types locally. It sends no real values.
    - The column mapping also finds the category column of the bank, if the CSV has one. The app
      uses that column locally as a hint for the disposition and the category.
    - To give a category to each row, it sends each unique description one time only.

## Deferred

These items are out of scope now. The design must not block them.

- **Import rules.** I make a rule, such as "a description that contains X gets disposition Y and
  category Z". The import tests each row against my rules before the LLM step. A row that matches
  never goes to the LLM. The rules live in DynamoDB, not in the code.
- **Memory of past choices.** The import uses the category that I chose before for the same
  description, and it does not ask the LLM.
- **A stored column mapping for each account.** The LLM then sees each bank format one time only.
- **A retirement-limit estimate.** A rough guess, from the payments, of whether we reach the
  annual 401k limit.
- **A paystub upload.** An LLM reads a paystub PDF and fills in the payment.
- **A link from a savings deduction to an account** of the net worth record.
- **Actual income against the plan.**

## Cross-cutting

- **Audit scope.** The audit log records every change to a budget target. This includes a pin, an
  unpin, and a pin that the app drops during a deactivation. It also records a hard delete of a
  category (Spec 16), and every change to the income plan (Spec 39). It does not record an insert, an update, or a delete of a transaction, and
  this holds even through the override. The trade-off is that an override change to a transaction
  in a locked month leaves no direct trace. This is acceptable, because the budget rows keep the
  history at the level of the category.
- **Audit entries.** Each entry records the user, an explanation that a person can read, and
  whether the user used the override. A budget-target entry also records each changed field,
  before and after. A hard-delete entry records counts instead — how many rows the app destroyed —
  because the whole category is gone.
- **Override behavior.** The override is a privileged action. No one may get past the locked-month
  rules by a change to the client code, or by a skip of a UI toggle. The override applies only to
  a change. Any signed-in user may read without limit.
- **Notifications on bulk failures.** When the app rejects a batch, such as a CSV commit or a
  change across several months, one response must report every row and every month at fault. It
  must not report them one at a time.
