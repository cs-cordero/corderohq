import base64
import csv
import io
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, assert_never
from zoneinfo import ZoneInfo

from corderohq.accounts import CashFlowRole, parse_create_account_args, parse_update_account_args, validate_asset_class
from corderohq.aws.dynamodb import (
    AccountTable,
    AuditAction,
    BudgetAuditLogTable,
    BudgetDensifyAdapter,
    BudgetTable,
    BudgetTargetAmounts,
    CategoryGroupTable,
    CategoryTable,
    IncomeSourceTable,
    NetWorthSnapshotTable,
    PlannedIncomeTable,
    ProfileTable,
    RecurringIncomeDensifyAdapter,
    RecurringIncomeValues,
    TransactionsTable,
    compute_income_plan,
    compute_summary,
    get_budget_treatment,
    get_cash_flow_role,
    new_recurring_income_id,
)
from corderohq.budget.auth import is_budget_admin
from corderohq.budget.csv_import import (
    assign_fingerprints,
    bank_category_hint,
    categorize_transactions,
    detect_column_types,
    map_columns,
    parse_amount,
    parse_date,
    spending_account_needs_flip,
    suggest_budget_treatments,
)
from corderohq.budget.densify import (
    carry_map,
    densify,
    next_year_month,
    prev_year_month,
    resolve_future_targets,
    value_at_or_before,
    walk_back_for,
)
from corderohq.budget.editability import EditabilityState, editability
from corderohq.budget.income import (
    ClearPin,
    ClearRecurringIncome,
    CreateOneTimeIncomeArgs,
    DeleteOneTimeIncomeArgs,
    OneTimeIncome,
    RecurringIncome,
    RecurringIncomeChange,
    SetPin,
    SetRecurringIncome,
    UpdateOneTimeIncomeArgs,
    parse_create_income_source_args,
    parse_one_time_income,
    parse_one_time_income_args,
    parse_recurring_income_changes,
    parse_update_income_source_args,
)
from corderohq.budget.transaction_args import (
    CreateTransactionArgs,
    parse_create_transaction_args,
    parse_update_transaction_args,
)
from corderohq.budget.treatment import BudgetTreatment, Classification, parse_classification
from corderohq.common import FieldChange, Keep, Remove, SetTo, resolve_change
from corderohq.networth.snapshots import build_history, build_month_view, compute_prefill
from corderohq.util import YEAR_MONTH_PATTERN, get_env_var

LOGGER = logging.getLogger(__name__)

_CATEGORY_GROUP_TABLE = CategoryGroupTable(get_env_var("CATEGORY_GROUP_TABLE_NAME"))
_CATEGORY_TABLE = CategoryTable(get_env_var("CATEGORY_TABLE_NAME"))
_BUDGET_TABLE = BudgetTable(get_env_var("BUDGET_TABLE_NAME"))
_TRANSACTIONS_TABLE = TransactionsTable(get_env_var("TRANSACTIONS_TABLE_NAME"))
_INCOME_SOURCE_TABLE = IncomeSourceTable(get_env_var("INCOME_SOURCE_TABLE_NAME"))
_PLANNED_INCOME_TABLE = PlannedIncomeTable(get_env_var("PLANNED_INCOME_TABLE_NAME"))
_AUDIT_LOG_TABLE = BudgetAuditLogTable(get_env_var("BUDGET_AUDIT_LOG_TABLE_NAME"))
_PROFILE_TABLE = ProfileTable(get_env_var("PROFILE_TABLE_NAME"))
_ACCOUNT_TABLE = AccountTable(get_env_var("ACCOUNT_TABLE_NAME"))
_NETWORTH_SNAPSHOT_TABLE = NetWorthSnapshotTable(get_env_var("NETWORTH_SNAPSHOT_TABLE_NAME"))
_STAGE = get_env_var("STAGE")

_SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


def _now_utc() -> datetime:
    """Return the real UTC time. This sits in its own function so a test can replace it."""

    return datetime.now(tz=UTC)


_BUDGET_TZ = ZoneInfo("America/New_York")


def _year_month_of(now: datetime) -> str:
    """Return the year-month of `now` in ET, because the budget month changes at midnight ET."""

    et = now.astimezone(_BUDGET_TZ)
    return f"{et.year:04d}-{et.month:02d}"


def _resolve_now(event: dict[str, Any]) -> datetime:
    """Return the "now" of this request.

    Outside prod, the `X-Simulated-Date` header can set a future date, as an ISO-8601 date or datetime. A date gets the
    time noon UTC. A past or invalid value is ignored, because a past date would write wrong rows.
    """

    if _STAGE != "prod":
        headers = event.get("headers") or {}
        raw = headers.get("x-simulated-date") or headers.get("X-Simulated-Date")
        if raw:
            parsed: datetime | None = None
            # Try a date on its own first. In Python 3.11 and later,
            # `datetime.fromisoformat` accepts a bare date such as "2026-08-01", and it
            # returns midnight. But midnight UTC falls on the day before in any zone west
            # of UTC, and ET is UTC-4 or UTC-5. _year_month_of would then give the wrong
            # month. Noon UTC falls inside the chosen day in every zone from UTC-11 to
            # UTC+11, so a date on its own goes to noon.
            try:
                parsed = datetime.strptime(raw, "%Y-%m-%d").replace(hour=12, tzinfo=UTC)
            except ValueError:
                try:
                    parsed = datetime.fromisoformat(raw)
                except ValueError:
                    LOGGER.error("Ignoring invalid X-Simulated-Date header: %s", raw)
            if parsed is not None:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                real_now = _now_utc()
                if parsed < real_now:
                    LOGGER.error(
                        "Ignoring X-Simulated-Date in the past: %s (real now: %s)",
                        parsed.isoformat(),
                        real_now.isoformat(),
                    )
                    return real_now
                LOGGER.info("X-Simulated-Date applied: %s", parsed.isoformat())
                return parsed
    return _now_utc()


def _ensure_hydrated(now: datetime) -> None:
    """Make each month dense, up to and including the year-month of `now`.

    Call this before any work that needs the dense rows of past and current months.
    """

    current_ym = _year_month_of(now)
    densify(
        BudgetDensifyAdapter(_BUDGET_TABLE),
        [c["categoryId"] for c in _CATEGORY_TABLE.get_active_categories()],
        current_ym,
        cold_start_value=0,
    )
    recurring_incomes = RecurringIncomeDensifyAdapter(_PLANNED_INCOME_TABLE)
    densify(recurring_incomes, recurring_incomes.recurring_income_ids(), current_ym, cold_start_value=None)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Route API Gateway HTTP API requests."""

    try:
        # Work out the effective "now" one time here, and pass it to every handler that
        # needs it. In dev it obeys the X-Simulated-Date header. In prod it always uses the
        # real time.
        now = _resolve_now(event)

        method = event["requestContext"]["http"]["method"]
        path = event["rawPath"]

        user = _extract_user(event)
        LOGGER.info("Request from %s: %s %s", user["email"] or user["sub"] or "unknown", method, path)

        # Category groups
        if path == "/api/category-groups" and method == "GET":
            return _get_category_groups()
        elif path == "/api/category-groups" and method == "POST":
            return _post_category_group(event)
        elif path == "/api/category-groups/reorder" and method == "POST":
            return _post_category_groups_reorder(event)
        elif path.startswith("/api/category-groups/") and method == "PUT":
            group_id = path.split("/")[-1]
            return _put_category_group(event, group_id)
        elif path.startswith("/api/category-groups/") and method == "DELETE":
            group_id = path.split("/")[-1]
            return _delete_category_group(group_id)

        # Categories
        elif path == "/api/categories" and method == "GET":
            return _get_categories(event)
        elif path == "/api/categories" and method == "POST":
            return _post_category(event, now)
        elif path == "/api/categories/reorder" and method == "POST":
            return _post_categories_reorder(event)
        elif path.startswith("/api/categories/") and method == "GET":
            category_id = path.split("/")[-1]
            return _get_category(event, category_id, now)
        elif path.startswith("/api/categories/") and method == "PUT":
            category_id = path.split("/")[-1]
            return _put_category(event, category_id)
        elif path.startswith("/api/categories/") and path.endswith("/reactivate") and method == "POST":
            category_id = path.split("/")[-2]
            return _reactivate_category(category_id)
        elif path.startswith("/api/categories/") and path.endswith("/deactivate") and method == "POST":
            category_id = path.split("/")[-2]
            return _deactivate_category(event, category_id, user, now)
        elif path.startswith("/api/categories/") and method == "DELETE":
            category_id = path.split("/")[-1]
            return _hard_delete_category(event, category_id, user)

        # Budget
        elif path.startswith("/api/budget/") and method == "GET":
            year_month = path.split("/")[-1]
            return _get_budget(year_month, now)
        elif path.startswith("/api/budget/") and path.endswith("/replace") and method == "POST":
            year_month = path.split("/")[-2]
            return _post_budget_replace(event, year_month, user, now)
        elif path.startswith("/api/budget/") and path.endswith("/update") and method == "POST":
            year_month = path.split("/")[-2]
            return _post_budget_update(event, year_month, user, now)

        # Transactions
        elif path == "/api/transactions/upload" and method == "POST":
            return _post_transactions_upload(event, now)
        elif path == "/api/transactions/commit" and method == "POST":
            return _post_transactions_commit(event, now)
        elif path == "/api/transactions" and method == "POST":
            return _post_transaction(event, now)
        elif path == "/api/transactions" and method == "GET":
            return _get_transactions(event)
        elif path == "/api/transactions/update" and method == "POST":
            return _put_transaction(event, now)
        elif path == "/api/transactions/delete" and method == "POST":
            return _delete_transaction(event, now)

        # Income sources
        elif path == "/api/income-sources" and method == "GET":
            return _get_income_sources(event)
        elif path == "/api/income-sources" and method == "POST":
            return _post_income_source(event)
        elif path == "/api/income-sources/reorder" and method == "POST":
            return _post_income_sources_reorder(event)
        elif path.startswith("/api/income-sources/") and path.endswith("/deactivate") and method == "POST":
            income_source_id = path.split("/")[-2]
            return _deactivate_income_source(event, income_source_id, user, now)
        elif path.startswith("/api/income-sources/") and path.endswith("/reactivate") and method == "POST":
            income_source_id = path.split("/")[-2]
            return _reactivate_income_source(income_source_id)
        elif path.startswith("/api/income-sources/") and method == "PUT":
            income_source_id = path.split("/")[-1]
            return _put_income_source(event, income_source_id)

        # The income plan of a month
        elif path.startswith("/api/income/") and method == "GET":
            year_month = path.split("/")[-1]
            return _get_income(year_month, now)
        elif path.startswith("/api/income/") and path.endswith("/replace") and method == "POST":
            year_month = path.split("/")[-2]
            return _post_income_replace(event, year_month, user, now)
        elif path.startswith("/api/income/") and path.endswith("/update") and method == "POST":
            year_month = path.split("/")[-2]
            return _post_income_update(event, year_month, user, now)
        elif path.startswith("/api/income/") and path.endswith("/one-time") and method == "POST":
            year_month = path.split("/")[-2]
            return _post_one_time(event, year_month, user, now)

        # Summary
        elif path == "/api/summary" and method == "GET":
            return _get_summary(event, now)

        # Audit log
        elif path == "/api/audit-log" and method == "GET":
            return _get_audit_log(event)

        # Household profile (net worth)
        elif path == "/api/profiles" and method == "GET":
            return _get_profiles()
        elif path == "/api/profiles" and method == "POST":
            return _post_profile(event)
        elif path.startswith("/api/profiles/") and path.endswith("/delete") and method == "POST":
            profile_id = path.split("/")[-2]
            return _delete_profile(profile_id)
        elif path.startswith("/api/profiles/") and method == "PUT":
            profile_id = path.split("/")[-1]
            return _put_profile(event, profile_id)

        # Accounts (net worth)
        elif path == "/api/accounts" and method == "GET":
            return _get_accounts(event)
        elif path == "/api/accounts" and method == "POST":
            return _post_account(event)
        elif path == "/api/accounts/reorder" and method == "POST":
            return _post_accounts_reorder(event)
        elif path.startswith("/api/accounts/") and path.endswith("/deactivate") and method == "POST":
            account_id = path.split("/")[-2]
            return _deactivate_account(account_id)
        elif path.startswith("/api/accounts/") and path.endswith("/reactivate") and method == "POST":
            account_id = path.split("/")[-2]
            return _reactivate_account(account_id)
        elif path.startswith("/api/accounts/") and method == "PUT":
            account_id = path.split("/")[-1]
            return _put_account(event, account_id)

        # The net worth snapshots. The fixed /history path comes before the /{YYYY-MM}
        # pattern, so that the app does not read "history" as a year-month.
        elif path == "/api/net-worth/history" and method == "GET":
            return _get_networth_history()
        elif path.startswith("/api/net-worth/") and method == "GET":
            year_month = path.split("/")[-1]
            return _get_networth_month(year_month, now)
        elif path.startswith("/api/net-worth/") and method == "POST":
            year_month = path.split("/")[-1]
            return _post_networth_month(event, year_month, now)

        else:
            return _response(404, "Not found")
    except Exception:
        LOGGER.exception("Unhandled error in personal finance handler")
        return _response(500, "Internal server error")


# --- Categories ---


def _get_categories(event: dict[str, Any]) -> dict[str, Any]:
    include_inactive = (event.get("queryStringParameters") or {}).get("include_inactive", "")
    if include_inactive.lower() == "true":
        categories = _CATEGORY_TABLE.get_all_categories()
    else:
        categories = _CATEGORY_TABLE.get_active_categories()
    return _json_response(200, json.dumps(categories, default=_json_default))


def _post_category(event: dict[str, Any], now: datetime) -> dict[str, Any]:
    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")

    name = body.get("name", "")
    if not name:
        return _response(400, "name is required")

    # initialTarget is 0 by default, which means "no spend this month". A given value must be an
    # integer in millionths of a dollar, and it must not be negative.
    initial_target = body.get("initialTarget", 0)
    if not isinstance(initial_target, int) or initial_target < 0:
        return _response(400, "initialTarget must be a non-negative integer (millionths of a dollar)")

    group_id = body.get("groupId")
    if not isinstance(group_id, str) or not group_id:
        return _response(400, "groupId is required")
    if _CATEGORY_GROUP_TABLE.get_category_group_by_id(group_id) is None:
        return _response(400, f"groupId '{group_id}' does not exist")

    # Densify first. The new category then goes into a current month that is dense.
    _ensure_hydrated(now)

    try:
        item = _CATEGORY_TABLE.create_category(name, group_id)
    except ValueError as e:
        return _response(400, str(e))

    # A new category appears in the current month at once, so that a summary of the kind
    # Spec 1 describes shows it. A walk-back from a later month then finds initialTarget,
    # until the user pins a different amount. See "Category lifecycle" in the architecture.
    _BUDGET_TABLE.set_single_budget_target(
        _year_month_of(now), item["categoryId"], BudgetTargetAmounts(carried_amount=initial_target)
    )

    return _json_response(201, json.dumps(item, default=_json_default))


def _put_category(event: dict[str, Any], category_id: str) -> dict[str, Any]:
    """Rename a category, move it to a different group, or do both.

    The body is `{name?, groupId?}`, with at least one key. A moved category goes to the end of its new group.
    """

    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")

    name = body.get("name")
    new_group_id = body.get("groupId")
    if name is None and new_group_id is None:
        return _response(400, "name and/or groupId is required")

    item: dict[str, Any] | None
    if name is not None:
        if not name:
            return _response(400, "name must not be empty")
        try:
            item = _CATEGORY_TABLE.update_category(category_id, name)
        except ValueError as e:
            return _response(400, str(e))
        if item is None:
            return _response(404, "Category not found")
    else:
        item = _CATEGORY_TABLE.get_category_by_id(category_id)
        if item is None:
            return _response(404, "Category not found")

    if new_group_id is not None:
        if not isinstance(new_group_id, str) or not new_group_id:
            return _response(400, "groupId must be a non-empty string")
        if _CATEGORY_GROUP_TABLE.get_category_group_by_id(new_group_id) is None:
            return _response(400, f"groupId '{new_group_id}' does not exist")
        item = _CATEGORY_TABLE.move_to_group(category_id, new_group_id)

    return _json_response(200, json.dumps(item, default=_json_default))


def _deactivate_category(
    event: dict[str, Any], category_id: str, user: dict[str, str], now: datetime
) -> dict[str, Any]:
    """Set a category to inactive.

    Without `confirm: true`, return the future months that have their own values for the category, and change
    nothing. With `confirm: true`, deactivate it, and delete those rows. Each deleted row writes one CLEAR audit entry
    for its carried amount, and one UNPIN entry for its pinned amount.
    """

    body = _parse_json_body(event) or {}
    confirm = bool(body.get("confirm", False))
    explanation = (body.get("explanation") or "").strip()

    if _CATEGORY_TABLE.get_category_by_id(category_id) is None:
        return _response(404, "Category not found")

    current_month = _year_month_of(now)
    rows = _BUDGET_TABLE.get_rows_for_category(category_id)
    affected_months = sorted({row["yearMonth"] for row in rows if row["yearMonth"] > current_month})

    if affected_months and not confirm:
        # Return affectedMonths, so that the UI can show a confirmation box. Change nothing yet.
        return _json_response(
            409,
            json.dumps(
                {
                    "error": "Category has future months with their own values; confirm to drop them",
                    "categoryId": category_id,
                    "affectedMonths": affected_months,
                }
            ),
        )

    if affected_months and not explanation:
        return _response(400, "explanation is required when dropping future values")

    # Delete the rows and write the audit entries before the active flag changes. The audit
    # trail then holds the amounts that were there before.
    for month in affected_months:
        existing = _BUDGET_TABLE.get_budget_targets(month)
        old = next((BudgetTargetAmounts.from_item(t) for t in existing if t["categoryId"] == category_id), None)
        _BUDGET_TABLE.delete_single_budget_target(month, category_id)
        if old is None:
            continue
        for action, name, before in (
            (AuditAction.CLEAR, "carriedAmount", old.carried_amount),
            (AuditAction.UNPIN, "pinnedAmount", old.pinned_amount),
        ):
            if before is None:
                continue
            _AUDIT_LOG_TABLE.write_entry(
                effective_year_month=month,
                category_id=category_id,
                action=action,
                explanation=explanation,
                user=user,
                changes={name: {"before": before, "after": None}},
            )

    item = _CATEGORY_TABLE.deactivate(category_id)
    return _json_response(
        200,
        json.dumps({"category": item, "affectedMonths": affected_months}, default=_json_default),
    )


def _reactivate_category(category_id: str) -> dict[str, Any]:
    item = _CATEGORY_TABLE.reactivate(category_id)
    if item is None:
        return _response(404, "Category not found")
    return _json_response(200, json.dumps(item, default=_json_default))


def _get_category(event: dict[str, Any], category_id: str, now: datetime) -> dict[str, Any]:
    """Answer GET /api/categories/{id}?deletion_preview=true, for admins only.

    Return the number of Budget rows, transactions, and locked months that a hard delete would erase.
    """

    query = event.get("queryStringParameters") or {}
    if query.get("deletion_preview", "").lower() != "true":
        return _response(400, "GET /api/categories/{id} requires ?deletion_preview=true")

    if not is_budget_admin(event, _STAGE):
        return _response(403, "Admin-only endpoint")

    item = _CATEGORY_TABLE.get_category_by_id(category_id)
    if item is None:
        return _response(404, "Category not found")

    budget_rows = _BUDGET_TABLE.get_rows_for_category(category_id)
    transaction_rows = _TRANSACTIONS_TABLE.get_rows_for_category(category_id)

    # A locked month is a past month with a Budget row for this category, where
    # year_month is at or before current_ym less the grace period. A month in grace is still
    # open to change, so we count it as not locked. This matches the editability function.
    current_ym = _year_month_of(now)
    past_months = sorted({row["yearMonth"] for row in budget_rows if row["yearMonth"] < current_ym})
    locked_months = [ym for ym in past_months if editability(ym, now) == EditabilityState.LOCKED]

    return _json_response(
        200,
        json.dumps(
            {
                "category": item,
                "deletionPreview": {
                    "budgetRows": len(budget_rows),
                    "transactions": len(transaction_rows),
                    "lockedMonthsAffected": locked_months,
                },
            },
            default=_json_default,
        ),
    )


def _hard_delete_category(event: dict[str, Any], category_id: str, user: dict[str, str]) -> dict[str, Any]:
    """Delete a category, and every row that names it, for ever. Admins only.

    The body needs `confirm: true`, a `confirmName` that matches the name, and an `explanation`. Write one
    CATEGORY_HARD_DELETE audit entry.
    """

    if not is_budget_admin(event, _STAGE):
        return _response(403, "Admin-only endpoint")

    body = _parse_json_body(event) or {}
    if body.get("confirm") is not True:
        return _response(400, "confirm: true is required for hard delete")
    confirm_name = body.get("confirmName")
    explanation = (body.get("explanation") or "").strip()
    if not explanation:
        return _response(400, "explanation is required")

    item = _CATEGORY_TABLE.get_category_by_id(category_id)
    if item is None:
        return _response(404, "Category not found")
    if confirm_name != item["name"]:
        return _response(400, f"confirmName must match the current category name exactly ('{item['name']}')")

    budget_rows_deleted = _BUDGET_TABLE.delete_all_for_category(category_id)
    transaction_rows_deleted = _TRANSACTIONS_TABLE.delete_all_transactions_for_category(category_id)
    _CATEGORY_TABLE.delete_category(category_id)

    # Write the audit entry last. The cascade then either finished in full, or it left
    # nothing in the log to explain. effectiveYearMonth is null, as the spec states.
    _AUDIT_LOG_TABLE.write_entry(
        effective_year_month=None,
        category_id=category_id,
        action=AuditAction.CATEGORY_HARD_DELETE,
        explanation=explanation,
        user=user,
        changes={
            "budgetRowsDeleted": budget_rows_deleted,
            "transactionsDeleted": transaction_rows_deleted,
            "name": item["name"],
        },
        override=True,
    )

    return _json_response(
        200,
        json.dumps(
            {
                "categoryId": category_id,
                "name": item["name"],
                "budgetRowsDeleted": budget_rows_deleted,
                "transactionsDeleted": transaction_rows_deleted,
            }
        ),
    )


# --- Category groups ---


def _get_category_groups() -> dict[str, Any]:
    return _json_response(200, json.dumps(_CATEGORY_GROUP_TABLE.get_all_category_groups(), default=_json_default))


def _post_category_group(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")
    name = body.get("name", "")
    if not name:
        return _response(400, "name is required")
    try:
        item = _CATEGORY_GROUP_TABLE.create_category_group(name)
    except ValueError as e:
        return _response(400, str(e))
    return _json_response(201, json.dumps(item, default=_json_default))


def _put_category_group(event: dict[str, Any], group_id: str) -> dict[str, Any]:
    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")
    name = body.get("name", "")
    if not name:
        return _response(400, "name is required")
    existing = _CATEGORY_GROUP_TABLE.get_category_group_by_id(group_id)
    if existing is None:
        return _response(404, "Group not found")
    if existing.get("system"):
        return _response(403, "System groups cannot be renamed")
    try:
        item = _CATEGORY_GROUP_TABLE.update_category_group(group_id, name)
    except ValueError as e:
        return _response(400, str(e))
    return _json_response(200, json.dumps(item, default=_json_default))


def _delete_category_group(group_id: str) -> dict[str, Any]:
    """Delete a group for ever.

    A system group cannot be deleted. An active category in the group blocks the delete with a 409. Each inactive
    category in the group moves to the Unassigned group.
    """

    existing = _CATEGORY_GROUP_TABLE.get_category_group_by_id(group_id)
    if existing is None:
        return _response(404, "Group not found")
    if existing.get("system"):
        return _response(403, "System groups cannot be deleted")

    cats_in_group = [c for c in _CATEGORY_TABLE.get_all_categories() if c.get("groupId") == group_id]
    active_blockers = [
        {"categoryId": c["categoryId"], "name": c["name"]} for c in cats_in_group if c.get("active", True)
    ]
    if active_blockers:
        return _json_response(
            409,
            json.dumps(
                {
                    "error": "Group has active categories — move them to another group first",
                    "blockingCategories": active_blockers,
                }
            ),
        )

    inactive_cats = [c for c in cats_in_group if not c.get("active", True)]
    if inactive_cats:
        unassigned = _CATEGORY_GROUP_TABLE.get_or_create_unassigned()
        for cat in inactive_cats:
            _CATEGORY_TABLE.move_to_group(cat["categoryId"], unassigned["groupId"])

    _CATEGORY_GROUP_TABLE.delete_category_group(group_id)
    return _json_response(
        200,
        json.dumps({"groupId": group_id, "movedDeactivatedCategories": len(inactive_cats)}),
    )


def _post_category_groups_reorder(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")
    order = body.get("order")
    if not isinstance(order, list) or not all(isinstance(g, str) for g in order):
        return _response(400, "'order' must be a list of groupId strings")
    _CATEGORY_GROUP_TABLE.reorder(order)
    return _json_response(200, json.dumps(_CATEGORY_GROUP_TABLE.get_all_category_groups(), default=_json_default))


def _post_categories_reorder(event: dict[str, Any]) -> dict[str, Any]:
    """Set the order of the categories in a group.

    The body is `{groupId, order: [categoryId, ...]}`. A category from a different group moves to `groupId`.
    """

    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")
    group_id = body.get("groupId")
    order = body.get("order")
    if not isinstance(group_id, str) or not group_id:
        return _response(400, "'groupId' is required")
    if not isinstance(order, list) or not all(isinstance(c, str) for c in order):
        return _response(400, "'order' must be a list of categoryId strings")
    if _CATEGORY_GROUP_TABLE.get_category_group_by_id(group_id) is None:
        return _response(404, "Group not found")
    _CATEGORY_TABLE.reorder_in_group(group_id, order)
    return _json_response(200, json.dumps(_CATEGORY_TABLE.get_all_categories(), default=_json_default))


# --- Budget ---


def _get_budget(year_month: str, now: datetime) -> dict[str, Any]:
    if not YEAR_MONTH_PATTERN.match(year_month):
        return _response(400, "Invalid month format, expected YYYY-MM")

    _ensure_hydrated(now)

    current_ym = _year_month_of(now)
    if year_month <= current_ym:
        # Dense month (past, grace, current). Stored rows are authoritative;
        # no walk-back needed.
        return _json_response(200, json.dumps(_BUDGET_TABLE.get_budget_targets(year_month), default=_json_default))

    # Future month: resolve effective values via per-category walk-back,
    # attaching a `pinned` flag so the UI can distinguish explicit pins from
    # carried values.
    active_ids = [c["categoryId"] for c in _CATEGORY_TABLE.get_active_categories()]
    targets = resolve_future_targets(BudgetDensifyAdapter(_BUDGET_TABLE), active_ids, year_month)
    return _json_response(200, json.dumps(targets, default=_json_default))


def _post_budget_replace(event: dict[str, Any], year_month: str, user: dict[str, str], now: datetime) -> dict[str, Any]:
    """Replace all budget targets of a dense month.

    A dense month is the current month, the grace month, or a locked month with an override. A future month is rejected;
    use /update for it. The body must hold each category that has a row, or a transaction that is not zero, in the
    month. Else a 409 lists the missing categories. A new or changed carried amount writes one CREATE or UPDATE audit
    entry. A set or removed pinned amount writes one PIN or UNPIN entry.

    Each target is `{categoryId, carriedAmount, pinnedAmount?}`. `carriedAmount` is a number. A `pinnedAmount` key sets
    the pinned amount (a number) or clears it (null). A target without the key keeps the stored pinned amount.
    """

    if not YEAR_MONTH_PATTERN.match(year_month):
        return _response(400, "Invalid month format, expected YYYY-MM")

    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")

    targets = body.get("targets")
    explanation = body.get("explanation", "").strip()
    if not isinstance(targets, list):
        return _response(400, "targets array is required")
    if not explanation:
        return _response(400, "explanation is required")

    parsed: dict[str, tuple[int, FieldChange[int]]] = {}
    for target in targets:
        if not isinstance(target, dict) or not isinstance(target.get("categoryId"), str):
            return _response(400, "Each target must have categoryId")
        carried = target.get("carriedAmount")
        if not _is_amount(carried):
            return _response(400, "carriedAmount must be an integer")
        try:
            parsed[target["categoryId"]] = (carried, _parse_amount_change(target, "pinnedAmount"))
        except ValueError as e:
            return _response(400, str(e))

    override_active, err = _resolve_override(event, body)
    if err:
        return err
    err = _check_year_month_editable(year_month, override_active, now)
    if err:
        return err

    _ensure_hydrated(now)

    if year_month > _year_month_of(now):
        return _json_response(
            409,
            json.dumps(
                {
                    "error": "/replace is for dense months only; use /update for future months",
                    "yearMonth": year_month,
                }
            ),
        )

    existing = _BUDGET_TABLE.get_budget_targets(year_month)
    existing_cat_ids = {t["categoryId"] for t in existing}
    transactions = _TRANSACTIONS_TABLE.list_transactions_for_month(year_month)
    transaction_category_ids = {
        t["categoryId"]
        for t in transactions
        if get_budget_treatment(t) == BudgetTreatment.BUDGET_IMPACTING and t.get("amount") != 0
    }
    required_cat_ids = existing_cat_ids | transaction_category_ids

    missing = sorted(required_cat_ids - set(parsed))
    if missing:
        return _json_response(
            409,
            json.dumps(
                {
                    "error": (
                        "body must include every category with an existing row or non-zero transaction in this month"
                    ),
                    "yearMonth": year_month,
                    "missingCategoryIds": missing,
                }
            ),
        )

    old_by_category = {t["categoryId"]: BudgetTargetAmounts.from_item(t) for t in existing}
    new_by_category = {
        cat_id: BudgetTargetAmounts(
            carried_amount=carried,
            pinned_amount=resolve_change(
                pinned_change, old.pinned_amount if (old := old_by_category.get(cat_id)) is not None else None
            ),
        )
        for cat_id, (carried, pinned_change) in parsed.items()
    }
    items = _BUDGET_TABLE.set_all_budget_targets(year_month, new_by_category)

    for cat_id, new in new_by_category.items():
        old = old_by_category.get(cat_id)
        old_carried = old.carried_amount if old is not None else None
        old_pinned = old.pinned_amount if old is not None else None
        entries: list[tuple[AuditAction, str, int | None, int | None]] = []
        if old_carried != new.carried_amount:
            action = AuditAction.CREATE if old_carried is None else AuditAction.UPDATE
            entries.append((action, "carriedAmount", old_carried, new.carried_amount))
        if old_pinned != new.pinned_amount:
            action = AuditAction.PIN if new.pinned_amount is not None else AuditAction.UNPIN
            entries.append((action, "pinnedAmount", old_pinned, new.pinned_amount))
        for action, name, before, after in entries:
            _AUDIT_LOG_TABLE.write_entry(
                effective_year_month=year_month,
                category_id=cat_id,
                action=action,
                explanation=explanation,
                user=user,
                changes={name: {"before": before, "after": after}},
                override=override_active,
            )

    return _json_response(200, json.dumps(items, default=_json_default))


def _post_budget_update(event: dict[str, Any], year_month: str, user: dict[str, str], now: datetime) -> dict[str, Any]:
    """Change some budget targets of a future month.

    The body is `{targets: [{categoryId, carriedAmount?: number | null, pinnedAmount?: number | null}], explanation}`.
    Each target needs `carriedAmount`, `pinnedAmount`, or both. A category that is not in `targets` does not change,
    and neither does a key that a target leaves out. Only a future month is accepted, also with an override.
    - `carriedAmount` is the month's own carried amount: a number sets it (UPDATE), and null removes it (CLEAR), so
      that the month inherits again. Later months carry it forward.
    - `pinnedAmount` is the amount of this month only: a number sets it (PIN), and null removes it (UNPIN). It never
      carries forward.
    A row with neither value left is deleted. The same value again does nothing, and writes no audit entry.

    Return `{targets, warnings: {laterCarriedChanges, matchesInheritedValue}}`, where `targets` holds the resolved
    values of the month.
    """

    if not YEAR_MONTH_PATTERN.match(year_month):
        return _response(400, "Invalid month format, expected YYYY-MM")

    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")

    targets = body.get("targets")
    explanation = body.get("explanation", "").strip()
    if not isinstance(targets, list):
        return _response(400, "targets array is required")
    if not explanation:
        return _response(400, "explanation is required")

    parsed: dict[str, tuple[FieldChange[int], FieldChange[int]]] = {}
    for target in targets:
        if not isinstance(target, dict) or not isinstance(target.get("categoryId"), str):
            return _response(400, "Each target must have categoryId")
        if "carriedAmount" not in target and "pinnedAmount" not in target:
            return _response(400, "Each target must have carriedAmount and/or pinnedAmount (each may be null)")
        try:
            parsed[target["categoryId"]] = (
                _parse_amount_change(target, "carriedAmount"),
                _parse_amount_change(target, "pinnedAmount"),
            )
        except ValueError as e:
            return _response(400, str(e))

    current_ym = _year_month_of(now)
    if year_month <= current_ym:
        return _json_response(
            409,
            json.dumps(
                {
                    "error": "/update is for future months only; use /replace for current/grace/locked months",
                    "yearMonth": year_month,
                }
            ),
        )
    state = editability(year_month, now)
    if state != EditabilityState.EDITABLE:
        return _json_response(
            409,
            json.dumps(
                {
                    "error": f"/update requires an editable future month; year-month is {state}",
                    "yearMonth": year_month,
                    "state": state,
                }
            ),
        )

    _ensure_hydrated(now)

    # Snapshot table state before any writes so walk-back warnings are computed
    # against the pre-write world.
    rows_before = {(r["yearMonth"], r["categoryId"]): BudgetTargetAmounts.from_item(r) for r in _BUDGET_TABLE.scan()}
    carried_before: dict[tuple[str, str], Any] = {
        key: amounts.carried_amount for key, amounts in rows_before.items() if amounts.carried_amount is not None
    }

    matches_inherited: list[str] = []

    for cat_id, (carried_change, pinned_change) in parsed.items():
        old = rows_before.get((year_month, cat_id))
        old_carried = old.carried_amount if old is not None else None
        old_pinned = old.pinned_amount if old is not None else None
        new_carried = resolve_change(carried_change, old_carried)
        new_pinned = resolve_change(pinned_change, old_pinned)
        if (new_carried, new_pinned) == (old_carried, old_pinned):
            continue

        if new_carried is None and new_pinned is None:
            _BUDGET_TABLE.delete_single_budget_target(year_month, cat_id)
        else:
            _BUDGET_TABLE.set_single_budget_target(
                year_month, cat_id, BudgetTargetAmounts(carried_amount=new_carried, pinned_amount=new_pinned)
            )

        for name, before, after, set_action, removed_action in (
            ("carriedAmount", old_carried, new_carried, AuditAction.UPDATE, AuditAction.CLEAR),
            ("pinnedAmount", old_pinned, new_pinned, AuditAction.PIN, AuditAction.UNPIN),
        ):
            if before == after:
                continue
            if name == "carriedAmount" and after is not None:
                # Warning: would the month inherit this same value without its own value?
                inherited = {k: v for k, v in carried_before.items() if k != (year_month, cat_id)}
                if walk_back_for(inherited, year_month, cat_id) == after:
                    matches_inherited.append(cat_id)
            _AUDIT_LOG_TABLE.write_entry(
                effective_year_month=year_month,
                category_id=cat_id,
                action=set_action if after is not None else removed_action,
                explanation=explanation,
                user=user,
                changes={name: {"before": before, "after": after}},
            )

    # Read the table again, then find the later months with their own carried amount, and the resolved month.
    rows_after = _BUDGET_TABLE.scan()
    touched_cat_ids = set(parsed)
    later_carried_changes = sorted(
        (
            {"categoryId": r["categoryId"], "yearMonth": r["yearMonth"]}
            for r in rows_after
            # Only an own carried amount stops a change from moving forward. A pinned amount does not.
            if r["categoryId"] in touched_cat_ids and r["yearMonth"] > year_month and "carriedAmount" in r
        ),
        key=lambda p: (p["yearMonth"], p["categoryId"]),
    )

    active_ids = [c["categoryId"] for c in _CATEGORY_TABLE.get_active_categories()]
    resolved = resolve_future_targets(BudgetDensifyAdapter(_BUDGET_TABLE), active_ids, year_month)

    return _json_response(
        200,
        json.dumps(
            {
                "targets": resolved,
                "warnings": {
                    "laterCarriedChanges": later_carried_changes,
                    "matchesInheritedValue": sorted(matches_inherited),
                },
            },
            default=_json_default,
        ),
    )


def _is_amount(value: Any) -> bool:
    """Return True if `value` is an amount: an integer in millionths of a dollar. A bool is not an amount."""

    return isinstance(value, int) and not isinstance(value, bool)


def _parse_amount_change(target: dict[str, Any], key: str) -> FieldChange[int]:
    """Return Keep() for an absent key, Remove() for null, and SetTo(amount) for an amount. Raise ValueError else."""

    if key not in target:
        return Keep()
    value = target[key]
    if value is None:
        return Remove()
    if not _is_amount(value):
        raise ValueError(f"{key} must be an integer or null")
    return SetTo(value)


# --- Income sources and the income plan ---


def _get_income_sources(event: dict[str, Any]) -> dict[str, Any]:
    include_inactive = (event.get("queryStringParameters") or {}).get("include_inactive") == "true"
    sources = _INCOME_SOURCE_TABLE.get_all_income_sources()
    if not include_inactive:
        sources = [s for s in sources if s.get("active", False)]
    return _json_response(200, json.dumps(sources, default=_json_default))


def _post_income_source(event: dict[str, Any]) -> dict[str, Any]:
    """Create an income source. Body: `{owners, label}`.

    `owners` is one or more personIds, the same as on an account. A source holds no money, so this makes no audit
    entry. Its recurring and one-time incomes come through the income plan routes.
    """

    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")
    try:
        args = parse_create_income_source_args(body)
    except ValueError as e:
        return _response(400, str(e))
    err = _check_owners_exist(args.owners)
    if err:
        return err
    source = _INCOME_SOURCE_TABLE.create_income_source(args)
    return _json_response(201, json.dumps(source, default=_json_default))


def _put_income_source(event: dict[str, Any], income_source_id: str) -> dict[str, Any]:
    """Change the label or the owners. This has no editability check, and no audit entry."""

    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")
    try:
        args = parse_update_income_source_args(body)
    except ValueError as e:
        return _response(400, str(e))
    match args.owners:
        case SetTo(value=owners):
            err = _check_owners_exist(owners)
            if err:
                return err
        case Keep():
            pass

    item = _INCOME_SOURCE_TABLE.update_income_source(income_source_id, args)
    if item is None:
        return _response(404, "Income source not found")
    return _json_response(200, json.dumps(item, default=_json_default))


def _post_income_sources_reorder(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")
    order = body.get("order")
    if not isinstance(order, list) or not all(isinstance(i, str) for i in order):
        return _response(400, "'order' must be a list of incomeSourceId strings")
    _INCOME_SOURCE_TABLE.reorder(order)
    return _json_response(200, json.dumps(_INCOME_SOURCE_TABLE.get_all_income_sources(), default=_json_default))


def _deactivate_income_source(
    event: dict[str, Any], income_source_id: str, user: dict[str, str], now: datetime
) -> dict[str, Any]:
    """Set an income source to inactive, and end each of its recurring incomes in the current month.

    Each recurring income of the source that has not ended gets the current month as its end month, with one
    RECURRING_INCOME_UPDATE entry. The current and past months do not change, so they keep their plan. Its own values in
    later months are deleted, with one RECURRING_INCOME_CLEAR entry for a carried value and one RECURRING_INCOME_UNPIN
    entry for a pinned value. Without `confirm: true`, a source with such values gets a 409 that lists them.
    """

    body = _parse_json_body(event) or {}
    confirm = bool(body.get("confirm", False))
    explanation = (body.get("explanation") or "").strip()

    if _INCOME_SOURCE_TABLE.get_income_source_by_id(income_source_id) is None:
        return _response(404, "Income source not found")

    current_ym = _year_month_of(now)
    store = RecurringIncomeDensifyAdapter(_PLANNED_INCOME_TABLE)
    carried = carry_map(store.scan_stored_values())
    pinned = carry_map(store.scan_pinned_values())
    keys = {key for (_, key), income in carried.items() if income.income_source_id == income_source_id}
    to_end = sorted(
        key
        for key in keys
        if (value := value_at_or_before(carried, current_ym, key)) is not None
        and (value.end_month is None or value.end_month > current_ym)
    )
    later_values = sorted(
        {(year_month, key) for (year_month, key) in [*carried, *pinned] if key in keys and year_month > current_ym}
    )
    if later_values and not confirm:
        return _json_response(
            409,
            json.dumps(
                {
                    "error": "Income source has recurring incomes with values in future months; confirm to drop them",
                    "incomeSourceId": income_source_id,
                    "futureValues": [{"yearMonth": ym, "plannedIncomeId": key} for ym, key in later_values],
                }
            ),
        )
    if (to_end or later_values) and not explanation:
        return _response(400, "explanation is required to end the recurring incomes of this source")

    for year_month, key in later_values:
        _PLANNED_INCOME_TABLE.delete_recurring_income(year_month, key)
        for action, field, before in (
            (AuditAction.RECURRING_INCOME_CLEAR, "recurringIncome", carried.get((year_month, key))),
            (AuditAction.RECURRING_INCOME_UNPIN, "pinnedRecurringIncome", pinned.get((year_month, key))),
        ):
            if before is None:
                continue
            _write_recurring_income_audit(
                year_month, action, key, income_source_id, before, None, explanation, user, False, field=field
            )
    for key in to_end:
        before = value_at_or_before(carried, current_ym, key)
        ended = replace(before, end_month=current_ym)
        _PLANNED_INCOME_TABLE.put_recurring_income(
            current_ym, key, RecurringIncomeValues(carried=ended, pinned=pinned.get((current_ym, key)))
        )
        _write_recurring_income_audit(
            current_ym,
            AuditAction.RECURRING_INCOME_UPDATE,
            key,
            income_source_id,
            before,
            ended,
            explanation,
            user,
            False,
        )

    item = _INCOME_SOURCE_TABLE.set_active(income_source_id, False)
    return _json_response(
        200,
        json.dumps(
            {
                "incomeSource": item,
                "endedRecurringIncomes": to_end,
                "deletedFutureValues": [{"yearMonth": ym, "plannedIncomeId": key} for ym, key in later_values],
            },
            default=_json_default,
        ),
    )


def _reactivate_income_source(income_source_id: str) -> dict[str, Any]:
    """Make an income source active again. Its ended recurring incomes stay ended."""

    item = _INCOME_SOURCE_TABLE.set_active(income_source_id, True)
    if item is None:
        return _response(404, "Income source not found")
    return _json_response(200, json.dumps(item, default=_json_default))


def _income_plan_response(year_month: str, now: datetime) -> dict[str, Any]:
    return compute_income_plan(_PLANNED_INCOME_TABLE, _INCOME_SOURCE_TABLE, year_month, now)


def _get_income(year_month: str, now: datetime) -> dict[str, Any]:
    if not YEAR_MONTH_PATTERN.match(year_month):
        return _response(400, "Invalid month format, expected YYYY-MM")
    _ensure_hydrated(now)
    return _json_response(200, json.dumps(_income_plan_response(year_month, now), default=_json_default))


def _check_recurring_income_changes(changes: list[RecurringIncomeChange], known_ids: set[str]) -> dict[str, Any] | None:
    """Return a 400 response if a change names an unknown recurring income or income source, else None."""

    for index, change in enumerate(changes):
        planned_income_id = change.planned_income_id
        if planned_income_id is not None and planned_income_id not in known_ids:
            return _response(400, f"recurring[{index}]: recurring income '{planned_income_id}' does not exist")
        match change:
            case SetRecurringIncome(income=income) | SetPin(income=income):
                if _INCOME_SOURCE_TABLE.get_income_source_by_id(income.income_source_id) is None:
                    return _response(
                        400, f"recurring[{index}]: income source '{income.income_source_id}' does not exist"
                    )
            case ClearRecurringIncome() | ClearPin():
                pass
            case _:
                assert_never(change)
    return None


def _check_end_month_rules(
    changes: list[RecurringIncomeChange],
    carried: dict[tuple[str, str], RecurringIncome],
    year_month: str,
    now: datetime,
    override_active: bool,
) -> dict[str, Any] | None:
    """Return a 400 response if a change breaks a rule of the end month, else None.

    An end month before the current month cannot be set, and a recurring income whose end month has passed cannot
    change. The admin override permits both.
    """

    if override_active:
        return None
    current_ym = _year_month_of(now)
    for index, change in enumerate(changes):
        match change:
            case SetRecurringIncome(income=income) | SetPin(income=income):
                if income.end_month is not None and income.end_month < current_ym:
                    return _response(400, f"recurring[{index}]: endMonth must not be before {current_ym}")
            case ClearRecurringIncome() | ClearPin():
                pass
            case _:
                assert_never(change)
        if change.planned_income_id is None:
            continue
        before = value_at_or_before(carried, year_month, change.planned_income_id)
        if before is not None and before.end_month is not None and before.end_month < current_ym:
            return _response(
                400,
                f"recurring[{index}]: this recurring income ended in {before.end_month}; only an admin can change it",
            )
    return None


def _write_recurring_income_row(
    year_month: str,
    key: str,
    carried: dict[tuple[str, str], RecurringIncome],
    pinned: dict[tuple[str, str], RecurringIncome],
) -> None:
    """Write the item of one recurring income in one month, from its carried value and its pinned value.

    A month with neither value has no item.
    """

    carried_value = carried.get((year_month, key))
    pinned_value = pinned.get((year_month, key))
    if carried_value is None and pinned_value is None:
        _PLANNED_INCOME_TABLE.delete_recurring_income(year_month, key)
    else:
        _PLANNED_INCOME_TABLE.put_recurring_income(
            year_month, key, RecurringIncomeValues(carried=carried_value, pinned=pinned_value)
        )


def _post_income_replace(event: dict[str, Any], year_month: str, user: dict[str, str], now: datetime) -> dict[str, Any]:
    """Write the recurring incomes of a dense month (current, grace, or locked through the override).

    Body: `{recurring: [{action, plannedIncomeId?, recurringIncome?}], explanation, override?}`. The body must hold each
    recurring income with a carried value in that month.
    - SET writes the carried value (RECURRING_INCOME_UPDATE). With no `plannedIncomeId`, it creates a new recurring
      income (RECURRING_INCOME_CREATE).
    - SET_PIN and CLEAR_PIN set or remove the pinned value of this month (RECURRING_INCOME_PIN or
      RECURRING_INCOME_UNPIN).
    - CLEAR is only for a future month.
    The same value again is a no-op with no audit entry. See `_check_end_month_rules` for the rules of the end month.
    """

    if not YEAR_MONTH_PATTERN.match(year_month):
        return _response(400, "Invalid month format, expected YYYY-MM")
    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")
    explanation = (body.get("explanation") or "").strip()
    if not explanation:
        return _response(400, "explanation is required")
    try:
        changes = parse_recurring_income_changes(body)
    except ValueError as e:
        return _response(400, str(e))
    if any(isinstance(change, ClearRecurringIncome) for change in changes):
        return _response(400, "CLEAR is only for a future month; use /update")

    override_active, err = _resolve_override(event, body)
    if err:
        return err
    err = _check_year_month_editable(year_month, override_active, now)
    if err:
        return err

    _ensure_hydrated(now)

    if year_month > _year_month_of(now):
        return _json_response(
            409,
            json.dumps(
                {
                    "error": "/replace is for dense months only; use /update for future months",
                    "yearMonth": year_month,
                }
            ),
        )

    store = RecurringIncomeDensifyAdapter(_PLANNED_INCOME_TABLE)
    carried = carry_map(store.scan_stored_values())
    pinned = carry_map(store.scan_pinned_values())
    err = _check_recurring_income_changes(changes, {key for _, key in carried})
    if err:
        return err
    err = _check_end_month_rules(changes, carried, year_month, now, override_active)
    if err:
        return err

    with_value = sorted(key for (ym, key) in carried if ym == year_month)
    missing = sorted(set(with_value) - {change.planned_income_id for change in changes})
    if missing:
        return _json_response(
            409,
            json.dumps(
                {
                    "error": "body must include every recurring income with a value in this month",
                    "yearMonth": year_month,
                    "missingPlannedIncomeIds": missing,
                }
            ),
        )

    for change in changes:
        match change:
            case SetRecurringIncome(planned_income_id=planned_income_id, income=income):
                key = planned_income_id or new_recurring_income_id()
                before = carried.get((year_month, key))
                if before == income:
                    continue
                carried[(year_month, key)] = income
                _write_recurring_income_row(year_month, key, carried, pinned)
                action = (
                    AuditAction.RECURRING_INCOME_CREATE
                    if planned_income_id is None
                    else AuditAction.RECURRING_INCOME_UPDATE
                )
                _write_recurring_income_audit(
                    year_month, action, key, income.income_source_id, before, income, explanation, user, override_active
                )
            case SetPin(planned_income_id=key, income=income):
                pinned_before = pinned.get((year_month, key))
                if pinned_before == income:
                    continue
                pinned[(year_month, key)] = income
                _write_recurring_income_row(year_month, key, carried, pinned)
                _write_recurring_income_audit(
                    year_month,
                    AuditAction.RECURRING_INCOME_PIN,
                    key,
                    income.income_source_id,
                    pinned_before,
                    income,
                    explanation,
                    user,
                    override_active,
                    field="pinnedRecurringIncome",
                )
            case ClearPin(planned_income_id=key):
                pinned_before = pinned.pop((year_month, key), None)
                if pinned_before is None:
                    continue
                _write_recurring_income_row(year_month, key, carried, pinned)
                _write_recurring_income_audit(
                    year_month,
                    AuditAction.RECURRING_INCOME_UNPIN,
                    key,
                    pinned_before.income_source_id,
                    pinned_before,
                    None,
                    explanation,
                    user,
                    override_active,
                    field="pinnedRecurringIncome",
                )
            case ClearRecurringIncome():
                pass
            case _:
                assert_never(change)

    return _json_response(200, json.dumps(_income_plan_response(year_month, now), default=_json_default))


def _post_income_update(event: dict[str, Any], year_month: str, user: dict[str, str], now: datetime) -> dict[str, Any]:
    """Change some recurring incomes of a future month.

    The same rules as the budget /update: the month must be EDITABLE and later than the current month. Body:
    `{recurring: [{action, plannedIncomeId?, recurringIncome?}], explanation, override?}`. A recurring income left out
    of the body does not change. The override only lifts the rules of the end month (see `_check_end_month_rules`).
    - SET writes the month's own carried value (RECURRING_INCOME_UPDATE). With no `plannedIncomeId`, it creates a new
      recurring income from this month on (RECURRING_INCOME_CREATE).
    - CLEAR removes the month's own carried value, so that the month inherits again (RECURRING_INCOME_CLEAR).
    - SET_PIN and CLEAR_PIN set or remove the pinned value of this month (RECURRING_INCOME_PIN or
      RECURRING_INCOME_UNPIN).
    The same value again is a no-op with no audit entry.

    Returns `{incomePlan: <the month, as GET /api/income>, warnings: {laterCarriedChanges, matchesInheritedValue}}`.
    """

    if not YEAR_MONTH_PATTERN.match(year_month):
        return _response(400, "Invalid month format, expected YYYY-MM")
    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")
    explanation = (body.get("explanation") or "").strip()
    if not explanation:
        return _response(400, "explanation is required")
    try:
        changes = parse_recurring_income_changes(body)
    except ValueError as e:
        return _response(400, str(e))
    override_active, err = _resolve_override(event, body)
    if err:
        return err

    if year_month <= _year_month_of(now):
        return _json_response(
            409,
            json.dumps(
                {
                    "error": "/update is for future months only; use /replace for current/grace/locked months",
                    "yearMonth": year_month,
                }
            ),
        )
    state = editability(year_month, now)
    if state != EditabilityState.EDITABLE:
        return _json_response(
            409,
            json.dumps(
                {
                    "error": f"/update requires an editable future month; year-month is {state}",
                    "yearMonth": year_month,
                    "state": state,
                }
            ),
        )

    _ensure_hydrated(now)

    # Read the plan before any write, so that the warnings compare with the plan as it was.
    store = RecurringIncomeDensifyAdapter(_PLANNED_INCOME_TABLE)
    carried = carry_map(store.scan_stored_values())
    pinned = carry_map(store.scan_pinned_values())
    err = _check_recurring_income_changes(changes, {key for _, key in carried})
    if err:
        return err
    err = _check_end_month_rules(changes, carried, year_month, now, override_active)
    if err:
        return err
    matches_inherited: list[str] = []
    touched_ids: set[str] = set()

    for change in changes:
        match change:
            case SetRecurringIncome(planned_income_id=planned_income_id, income=income):
                key = planned_income_id or new_recurring_income_id()
                touched_ids.add(key)
                held = (year_month, key) in carried
                before = carried.get((year_month, key))
                if held and before == income:
                    continue
                carried[(year_month, key)] = income
                _write_recurring_income_row(year_month, key, carried, pinned)
                if planned_income_id is None:
                    action = AuditAction.RECURRING_INCOME_CREATE
                else:
                    action = AuditAction.RECURRING_INCOME_UPDATE
                    if walk_back_for(carried, year_month, key) == income:
                        matches_inherited.append(key)
                _write_recurring_income_audit(
                    year_month, action, key, income.income_source_id, before, income, explanation, user, override_active
                )
            case ClearRecurringIncome(planned_income_id=key):
                touched_ids.add(key)
                if (year_month, key) not in carried:
                    continue
                before = carried.pop((year_month, key))
                _write_recurring_income_row(year_month, key, carried, pinned)
                _write_recurring_income_audit(
                    year_month,
                    AuditAction.RECURRING_INCOME_CLEAR,
                    key,
                    before.income_source_id,
                    before,
                    None,
                    explanation,
                    user,
                    override_active,
                )
            case SetPin(planned_income_id=key, income=income):
                pinned_before = pinned.get((year_month, key))
                if pinned_before == income:
                    continue
                pinned[(year_month, key)] = income
                _write_recurring_income_row(year_month, key, carried, pinned)
                _write_recurring_income_audit(
                    year_month,
                    AuditAction.RECURRING_INCOME_PIN,
                    key,
                    income.income_source_id,
                    pinned_before,
                    income,
                    explanation,
                    user,
                    override_active,
                    field="pinnedRecurringIncome",
                )
            case ClearPin(planned_income_id=key):
                pinned_before = pinned.pop((year_month, key), None)
                if pinned_before is None:
                    continue
                _write_recurring_income_row(year_month, key, carried, pinned)
                _write_recurring_income_audit(
                    year_month,
                    AuditAction.RECURRING_INCOME_UNPIN,
                    key,
                    pinned_before.income_source_id,
                    pinned_before,
                    None,
                    explanation,
                    user,
                    override_active,
                    field="pinnedRecurringIncome",
                )
            case _:
                assert_never(change)

    # A later month with its own carried value keeps it, so a change in this month does not reach it. A later pinned
    # value does not stop a change.
    later_carried_changes = sorted(
        (
            {"plannedIncomeId": key, "yearMonth": ym}
            for ym, key, _ in RecurringIncomeDensifyAdapter(_PLANNED_INCOME_TABLE).scan_stored_values()
            if key in touched_ids and ym > year_month
        ),
        key=lambda p: (p["yearMonth"], p["plannedIncomeId"]),
    )

    return _json_response(
        200,
        json.dumps(
            {
                "incomePlan": _income_plan_response(year_month, now),
                "warnings": {
                    "laterCarriedChanges": later_carried_changes,
                    "matchesInheritedValue": sorted(matches_inherited),
                },
            },
            default=_json_default,
        ),
    )


def _write_recurring_income_audit(
    year_month: str,
    action: AuditAction,
    planned_income_id: str,
    income_source_id: str | None,
    before: RecurringIncome | None,
    after: RecurringIncome | None,
    explanation: str,
    user: dict[str, str],
    override_active: bool,
    field: str = "recurringIncome",
) -> None:
    """Write the audit entry of one change to a recurring income. A value of None is no value.

    `field` names the change: `recurringIncome` for the carried value, or `pinnedRecurringIncome`.
    """

    _AUDIT_LOG_TABLE.write_entry(
        effective_year_month=year_month,
        category_id=None,
        action=action,
        explanation=explanation,
        user=user,
        changes={
            field: {
                "before": before.to_item() if before is not None else None,
                "after": after.to_item() if after is not None else None,
            }
        },
        override=override_active,
        income_source_id=income_source_id,
        planned_income_id=planned_income_id,
    )


def _post_one_time(event: dict[str, Any], year_month: str, user: dict[str, str], now: datetime) -> dict[str, Any]:
    """Create, change, or delete a one-time income. One route does all three, from the body:

    - Create: `{label, incomeSourceId?, gross, deductions, explanation, override?}` (no `plannedIncomeId`)
    - Change: `{plannedIncomeId, label, incomeSourceId?, gross, deductions, explanation, override?}`
    - Delete: `{plannedIncomeId, delete: true, explanation, override?}`

    The month must be EDITABLE or GRACE, or LOCKED with the override. A future month is permitted. Each call makes one
    ONE_TIME_INCOME_CREATE, ONE_TIME_INCOME_UPDATE, or ONE_TIME_INCOME_DELETE entry. The entry holds the label, because
    the income can be deleted later.
    """

    if not YEAR_MONTH_PATTERN.match(year_month):
        return _response(400, "Invalid month format, expected YYYY-MM")
    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")
    explanation = (body.get("explanation") or "").strip()
    if not explanation:
        return _response(400, "explanation is required")
    try:
        args = parse_one_time_income_args(body)
    except ValueError as e:
        return _response(400, str(e))

    override_active, err = _resolve_override(event, body)
    if err:
        return err
    err = _check_year_month_editable(year_month, override_active, now)
    if err:
        return err

    match args:
        case CreateOneTimeIncomeArgs():
            return _create_one_time_income(year_month, args, explanation, user, override_active)
        case UpdateOneTimeIncomeArgs():
            return _update_one_time_income(year_month, args, explanation, user, override_active)
        case DeleteOneTimeIncomeArgs():
            return _delete_one_time_income(year_month, args, explanation, user, override_active)
        case _:
            assert_never(args)


def _create_one_time_income(
    year_month: str, args: CreateOneTimeIncomeArgs, explanation: str, user: dict[str, str], override_active: bool
) -> dict[str, Any]:
    """Create a one-time income, and write a ONE_TIME_INCOME_CREATE audit entry."""

    err = _check_one_time_income_source(args.income)
    if err:
        return err
    item = _PLANNED_INCOME_TABLE.create_one_time_income(year_month, args.income)
    _write_one_time_income_audit(
        year_month,
        AuditAction.ONE_TIME_INCOME_CREATE,
        str(item["plannedIncomeId"]),
        before=None,
        after=args.income,
        explanation=explanation,
        user=user,
        override_active=override_active,
    )
    return _json_response(201, json.dumps(item, default=_json_default))


def _update_one_time_income(
    year_month: str, args: UpdateOneTimeIncomeArgs, explanation: str, user: dict[str, str], override_active: bool
) -> dict[str, Any]:
    """Change a one-time income, and write a ONE_TIME_INCOME_UPDATE audit entry."""

    stored = _PLANNED_INCOME_TABLE.get_planned_income(year_month, args.planned_income_id)
    if stored is None:
        return _response(404, "One-time income not found")
    err = _check_one_time_income_source(args.income)
    if err:
        return err
    item = _PLANNED_INCOME_TABLE.update_one_time_income(year_month, args.planned_income_id, args.income)
    _write_one_time_income_audit(
        year_month,
        AuditAction.ONE_TIME_INCOME_UPDATE,
        args.planned_income_id,
        before=parse_one_time_income(stored),
        after=args.income,
        explanation=explanation,
        user=user,
        override_active=override_active,
    )
    return _json_response(200, json.dumps(item, default=_json_default))


def _delete_one_time_income(
    year_month: str, args: DeleteOneTimeIncomeArgs, explanation: str, user: dict[str, str], override_active: bool
) -> dict[str, Any]:
    """Delete a one-time income, and write a ONE_TIME_INCOME_DELETE audit entry that keeps its value."""

    stored = _PLANNED_INCOME_TABLE.get_planned_income(year_month, args.planned_income_id)
    if stored is None:
        return _response(404, "One-time income not found")
    _PLANNED_INCOME_TABLE.delete_one_time_income(year_month, args.planned_income_id)
    _write_one_time_income_audit(
        year_month,
        AuditAction.ONE_TIME_INCOME_DELETE,
        args.planned_income_id,
        before=parse_one_time_income(stored),
        after=None,
        explanation=explanation,
        user=user,
        override_active=override_active,
    )
    return _json_response(200, json.dumps({"deleted": args.planned_income_id}, default=_json_default))


def _check_one_time_income_source(income: OneTimeIncome) -> dict[str, Any] | None:
    """Return a 400 response if the one-time income names an income source that does not exist, else None."""

    if income.income_source_id is None:
        return None
    if _INCOME_SOURCE_TABLE.get_income_source_by_id(income.income_source_id) is None:
        return _response(400, f"income source '{income.income_source_id}' does not exist")
    return None


def _write_one_time_income_audit(
    year_month: str,
    action: AuditAction,
    planned_income_id: str,
    *,
    before: OneTimeIncome | None,
    after: OneTimeIncome | None,
    explanation: str,
    user: dict[str, str],
    override_active: bool,
) -> None:
    """Write the audit entry of one change to a one-time income."""

    source = after or before
    _AUDIT_LOG_TABLE.write_entry(
        effective_year_month=year_month,
        category_id=None,
        action=action,
        explanation=explanation,
        user=user,
        changes={
            "oneTimeIncome": {
                "before": before.to_item() if before is not None else None,
                "after": after.to_item() if after is not None else None,
            }
        },
        override=override_active,
        income_source_id=source.income_source_id if source is not None else None,
        planned_income_id=planned_income_id,
    )


# --- Transactions ---


def _get_transactions(event: dict[str, Any]) -> dict[str, Any]:
    month = (event.get("queryStringParameters") or {}).get("month", "")
    if not month or not YEAR_MONTH_PATTERN.match(month):
        return _response(400, "query parameter 'month' is required (YYYY-MM)")

    transactions = _TRANSACTIONS_TABLE.list_transactions_for_month(month)
    return _json_response(200, json.dumps(transactions, default=_json_default))


def _check_account_exists(account_id: str | None) -> dict[str, Any] | None:
    """Return a 400 response if `account_id` is set and names no account. Else return None."""

    if account_id is not None and _ACCOUNT_TABLE.get_account_by_id(account_id) is None:
        return _response(400, f"accountId '{account_id}' does not exist")
    return None


def _post_transaction(event: dict[str, Any], now: datetime) -> dict[str, Any]:
    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")

    year_month = body.get("yearMonth")
    if not isinstance(year_month, str) or not YEAR_MONTH_PATTERN.match(year_month):
        return _response(400, "yearMonth is required, as YYYY-MM")
    try:
        new = parse_create_transaction_args(body, year_month=year_month)
    except ValueError as e:
        return _response(400, str(e))
    err = _check_account_exists(new.account_id)
    if err:
        return err

    override_active, err = _resolve_override(event, body)
    if err:
        return err
    err = _check_year_month_editable(year_month, override_active, now)
    if err:
        return err

    item = _TRANSACTIONS_TABLE.create_transaction(new)
    return _json_response(201, json.dumps(item, default=_json_default))


class RowIssue(StrEnum):
    """A problem with one CSV row that blocks the commit. The upload and the commit send the value."""

    MISSING_CATEGORY = "missing_category"
    LOCKED_MONTH = "locked_month"


def _validate_commit_rows(
    rows: list[tuple[Classification, str]],
    valid_category_ids: set[str],
    override_active: bool,
    now: datetime,
) -> list[dict[str, Any]]:
    """Return the issues of each row for /transactions/commit and /transactions/upload.

    Each row is `(classification, transactionDate)`. Each result is `{index, issues}`, and each issue is a RowIssue
    value. `missing_category` applies to a BUDGET_IMPACTING row with no known category, and to an OFF_BUDGET row with
    a category that does not exist. An override suppresses `locked_month`.
    """

    results: list[dict[str, Any]] = []
    for i, (classification, transaction_date) in enumerate(rows):
        issues: list[RowIssue] = []
        category_id = classification.category_id
        category_unknown = category_id is not None and category_id not in valid_category_ids
        if (classification.treatment == BudgetTreatment.BUDGET_IMPACTING and category_id is None) or category_unknown:
            issues.append(RowIssue.MISSING_CATEGORY)
        year_month = transaction_date[:7]
        if YEAR_MONTH_PATTERN.match(year_month):
            state = editability(year_month, now)
            if state == EditabilityState.LOCKED and not override_active:
                issues.append(RowIssue.LOCKED_MONTH)
        results.append({"index": i, "issues": issues})
    return results


def _post_transactions_commit(event: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Save all rows of a CSV import, or none of them.

    The body is `{rows: [{transactionDate, description, amount, categoryId?, budgetTreatment?, accountId?, note?,
    fingerprint?}, ...], override?}`. `categoryId` is necessary only for a BUDGET_IMPACTING row. If any row has an
    issue, return a 409 with the issues of all rows, and save nothing. A row whose fingerprint is saved already is not
    saved again, and the response lists its index in `skippedDuplicates`.
    """

    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")

    rows = body.get("rows")
    if not isinstance(rows, list) or not rows:
        return _response(400, "rows array is required and must be non-empty")

    override_active, err = _resolve_override(event, body)
    if err:
        return err

    new_rows: list[CreateTransactionArgs] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            return _response(400, f"row {i}: must be an object")
        try:
            # A missing category is a row issue (see _validate_commit_rows), not an error.
            year_month = str(row.get("transactionDate", ""))[:7]
            new = parse_create_transaction_args(row, year_month=year_month, require_category=False)
        except ValueError as e:
            return _response(400, f"row {i}: {e}")
        err = _check_account_exists(new.account_id)
        if err:
            return err
        new_rows.append(new)

    _ensure_hydrated(now)

    valid_category_ids = {c["categoryId"] for c in _CATEGORY_TABLE.get_all_categories()}
    validations = _validate_commit_rows(
        [(new.classification, new.transaction_date) for new in new_rows], valid_category_ids, override_active, now
    )
    invalid = [v for v in validations if v["issues"]]
    if invalid:
        return _json_response(
            409,
            json.dumps(
                {
                    "error": "commit aborted; one or more rows have validation issues",
                    "validations": validations,
                }
            ),
        )

    # Drop each row that the table holds already, by its fingerprint. A retry after a failure
    # then writes only the rows that are not there yet.
    existing_fingerprints = _existing_fingerprints({new.transaction_date[:7] for new in new_rows})
    skipped_duplicates = [i for i, new in enumerate(new_rows) if new.fingerprint in existing_fingerprints]
    to_create = [new for new in new_rows if new.fingerprint not in existing_fingerprints]
    created = _TRANSACTIONS_TABLE.batch_create_transaction(to_create) if to_create else []

    return _json_response(
        200,
        json.dumps(
            {"count": len(created), "transactions": created, "skippedDuplicates": skipped_duplicates},
            default=_json_default,
        ),
    )


def _saved_transactions(year_months: set[str]) -> list[dict[str, Any]]:
    """Return the saved transactions of the given months. A month that is not YYYY-MM is skipped."""

    result: list[dict[str, Any]] = []
    for year_month in sorted(year_months):
        if YEAR_MONTH_PATTERN.match(year_month):
            result.extend(_TRANSACTIONS_TABLE.list_transactions_for_month(year_month))
    return result


def _existing_fingerprints(year_months: set[str]) -> set[str]:
    """Return the fingerprints of the saved transactions in the given months."""

    return {str(t["fingerprint"]) for t in _saved_transactions(year_months) if isinstance(t.get("fingerprint"), str)}


def _months_around(rows: list[dict[str, Any]]) -> set[str]:
    """Return the month of each row, and the month before and after it.

    A date that the code can read gives its month. Otherwise the first 7 characters are used,
    the same as the commit does.
    """

    months: set[str] = set()
    for row in rows:
        parsed = parse_date(str(row["transactionDate"]))
        year_month = parsed.strftime("%Y-%m") if parsed is not None else str(row["transactionDate"])[:7]
        if YEAR_MONTH_PATTERN.match(year_month):
            months.update({prev_year_month(year_month), year_month, next_year_month(year_month)})
    return months


def _post_transactions_upload(event: dict[str, Any], now: datetime) -> dict[str, Any]:
    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")

    csv_data = body.get("csvData", "")
    if not csv_data:
        return _response(400, "csvData is required")

    # Each upload belongs to one account, and the account needs a cash flow role. The role and the
    # direction of the money set the suggested budget treatment of each row.
    account_id = body.get("accountId")
    if not isinstance(account_id, str) or not account_id:
        return _response(400, "accountId is required")
    account = _ACCOUNT_TABLE.get_account_by_id(account_id)
    if account is None:
        return _response(400, f"accountId '{account_id}' does not exist")
    if get_cash_flow_role(account) not in (CashFlowRole.SPENDING, CashFlowRole.HOLDING):
        return _response(400, "only a SPENDING or HOLDING account can take an upload; see the Accounts page")

    # Parse CSV
    reader = csv.reader(io.StringIO(csv_data))
    rows = list(reader)
    if len(rows) < 2:
        return _response(400, "CSV must have a header row and at least one data row")

    headers = rows[0]
    data_rows = rows[1:]

    # Step 1: the LLM finds which column is which. It gets the header row and a type for each
    # column, which the code finds locally. It gets no cell values.
    column_mapping = map_columns(headers, detect_column_types(headers, data_rows))

    date_col = column_mapping.get("date", "")
    desc_col = column_mapping.get("description", "")
    amount_col = column_mapping.get("amount", "")
    amount_invert = column_mapping.get("amount_invert", False) is True
    bank_col = column_mapping.get("bankCategory")

    if not date_col or not desc_col or not amount_col:
        return _response(400, "Could not identify required columns (date, description, amount)")

    # Validate column names exist in headers
    for col_name in (date_col, desc_col, amount_col):
        if col_name not in headers:
            return _response(400, f"Mapped column '{col_name}' not found in CSV headers")

    # Step 2: Extract transactions from CSV
    col_indices = {h: i for i, h in enumerate(headers)}
    date_idx = col_indices[date_col]
    desc_idx = col_indices[desc_col]
    amount_idx = col_indices[amount_col]
    # The bank's category column is optional. A name that is not a header is ignored.
    bank_idx = col_indices.get(bank_col) if isinstance(bank_col, str) else None

    parsed_transactions: list[dict[str, Any]] = []
    for row in data_rows:
        if len(row) <= max(date_idx, desc_idx, amount_idx):
            continue
        amount_float = parse_amount(row[amount_idx])
        if amount_float is None:
            continue
        bank_value = row[bank_idx].strip() if bank_idx is not None and bank_idx < len(row) else None
        parsed_transactions.append(
            {
                "transactionDate": row[date_idx].strip(),
                "description": row[desc_idx].strip(),
                "amount": round(amount_float * 1_000_000),
                "_bankHint": bank_category_hint(bank_value),
            }
        )

    if not parsed_transactions:
        return _response(400, "No valid transactions found in CSV")

    # The app stores money out as positive. Three checks set the sign, in order: the LLM's
    # guess from the header names; a local check for a spending account, where most rows are
    # money out; and the user's "flip all signs" button on the review screen.
    amounts_after_llm = [-t["amount"] if amount_invert else t["amount"] for t in parsed_transactions]
    is_spending_account = get_cash_flow_role(account) == CashFlowRole.SPENDING
    locally_flipped = is_spending_account and spending_account_needs_flip(amounts_after_llm)
    user_flipped = body.get("flipSigns") is True
    if amount_invert ^ locally_flipped ^ user_flipped:
        for transaction in parsed_transactions:
            transaction["amount"] = -transaction["amount"]

    # Step 3: the LLM gives a kind and a category to each unique description.
    categories = _CATEGORY_TABLE.get_active_categories()
    assignments = categorize_transactions([t["description"] for t in parsed_transactions], categories)
    for transaction, assignment in zip(parsed_transactions, assignments, strict=True):
        transaction["categoryId"] = assignment.get("categoryId") or ""
        transaction["categoryName"] = assignment.get("categoryName") or ""
        transaction["_kind"] = assignment.get("kind")

    # Load the saved transactions of each month of the upload, and of the month before and after
    # it. A transfer can cross the end of a month. The fingerprints mark each row that the table
    # holds already, such as from an earlier CSV with dates that overlap; the review screen leaves
    # those rows unselected. The pair match looks for the other side of a transfer.
    saved = _saved_transactions(_months_around(parsed_transactions))
    existing_fingerprints = {str(t["fingerprint"]) for t in saved if isinstance(t.get("fingerprint"), str)}

    # Step 4: suggest a budget treatment for each row, from the direction of the money.
    suggestions = suggest_budget_treatments(parsed_transactions, account, _ACCOUNT_TABLE.get_all_accounts(), saved)
    category_names = {str(c["categoryId"]): str(c["name"]) for c in categories}
    fingerprints = assign_fingerprints(parsed_transactions, account_id)
    for transaction, row_fingerprint, suggestion in zip(parsed_transactions, fingerprints, suggestions, strict=True):
        transaction["accountId"] = account_id
        transaction["fingerprint"] = row_fingerprint
        transaction["duplicate"] = row_fingerprint in existing_fingerprints
        transaction["budgetTreatment"] = suggestion.budget_treatment
        transaction["categoryId"] = suggestion.category_id
        transaction["categoryName"] = category_names.get(suggestion.category_id, "")
        transaction["suggestedBy"] = suggestion.suggested_by
        transaction["pairWith"] = suggestion.pair_with
        transaction["review"] = suggestion.review

    # Per-row pre-validation surfaces missing_category / locked_month issues so the
    # review UI can render specific annotations without round-tripping through
    # /commit just to learn what's wrong. `review` reasons do not block the commit.
    valid_category_ids = {c["categoryId"] for c in _CATEGORY_TABLE.get_all_categories()}
    classified = [
        (
            parse_classification(row.get("budgetTreatment"), row.get("categoryId"), require_category=False),
            str(row["transactionDate"]),
        )
        for row in parsed_transactions
    ]
    validations = _validate_commit_rows(classified, valid_category_ids, override_active=False, now=now)
    for validation, transaction in zip(validations, parsed_transactions, strict=True):
        validation["review"] = transaction.pop("review", [])
        # The kind and the bank hint are for the suggestion only. They do not go to the client.
        transaction.pop("_kind", None)
        transaction.pop("_bankHint", None)

    return _json_response(
        200,
        json.dumps(
            {
                "accountId": account_id,
                "signCheck": {
                    "llmInverted": amount_invert,
                    "locallyFlipped": locally_flipped,
                    "userFlipped": user_flipped,
                },
                "columnMapping": column_mapping,
                "transactions": parsed_transactions,
                "validations": validations,
            },
            default=_json_default,
        ),
    )


def _put_transaction(event: dict[str, Any], now: datetime) -> dict[str, Any]:
    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")

    year_month = body.get("yearMonth", "")
    sort_id = body.get("sortId", "")
    if not year_month or not sort_id:
        return _response(400, "yearMonth and sortId are required")

    override_active, err = _resolve_override(event, body)
    if err:
        return err
    err = _check_year_month_editable(year_month, override_active, now)
    if err:
        return err

    existing = _TRANSACTIONS_TABLE.get_transaction(year_month, sort_id)
    if existing is None:
        return _response(404, "Transaction not found")
    try:
        args = parse_update_transaction_args(body, existing)
    except ValueError as e:
        return _response(400, str(e))
    match args.account_id:
        case SetTo(value=account_id):
            err = _check_account_exists(account_id)
            if err:
                return err
        case Keep() | Remove():
            pass
    if args.changes_nothing():
        return _json_response(200, json.dumps(existing, default=_json_default))

    item = _TRANSACTIONS_TABLE.update_transaction(year_month, sort_id, args)
    return _json_response(200, json.dumps(item, default=_json_default))


def _delete_transaction(event: dict[str, Any], now: datetime) -> dict[str, Any]:
    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")

    items = body.get("items")
    if not isinstance(items, list) or len(items) == 0:
        return _response(400, "'items' array is required")

    for item in items:
        if not item.get("yearMonth") or not item.get("sortId"):
            return _response(400, "Each item must have yearMonth and sortId")

    override_active, err = _resolve_override(event, body)
    if err:
        return err
    err = _check_year_months_editable(items, "yearMonth", override_active, now)
    if err:
        return err

    deleted = _TRANSACTIONS_TABLE.batch_delete_transaction(items)
    return _json_response(200, json.dumps({"deleted": deleted}))


# --- Summary ---


def _get_summary(event: dict[str, Any], now: datetime) -> dict[str, Any]:
    month = (event.get("queryStringParameters") or {}).get("month", "")
    if not month or not YEAR_MONTH_PATTERN.match(month):
        return _response(400, "query parameter 'month' is required (YYYY-MM)")

    _ensure_hydrated(now)

    result = compute_summary(_BUDGET_TABLE, _TRANSACTIONS_TABLE, _CATEGORY_TABLE, month, now)
    # Left to assign: the planned net income minus every budget target. It uses the same
    # category membership as `totals.budgeted`. No transaction changes it.
    planned = int(compute_income_plan(_PLANNED_INCOME_TABLE, _INCOME_SOURCE_TABLE, month, now)["plannedNetIncome"])
    result["income"] = {"plannedNetIncome": planned, "leftToAssign": planned - int(result["totals"]["budgeted"])}
    return _json_response(200, json.dumps(result, default=_json_default))


# --- Audit log ---


def _get_audit_log(event: dict[str, Any]) -> dict[str, Any]:
    limit_str = (event.get("queryStringParameters") or {}).get("limit", "10")
    try:
        limit = max(1, min(int(limit_str), 200))
    except ValueError:
        limit = 10

    entries = _AUDIT_LOG_TABLE.read_recent_entries(limit)
    return _json_response(200, json.dumps(entries, default=_json_default))


# --- Household profile (net worth) ---


def _get_profiles() -> dict[str, Any]:
    return _json_response(200, json.dumps(_PROFILE_TABLE.list_profiles(), default=_json_default))


def _post_profile(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")

    name = body.get("name", "")
    if not name:
        return _response(400, "name is required")
    birth_year_month = body.get("birthYearMonth", "")
    if not birth_year_month:
        return _response(400, "birthYearMonth is required")

    try:
        item = _PROFILE_TABLE.create_profile(name, birth_year_month)
    except ValueError as e:
        return _response(400, str(e))
    return _json_response(201, json.dumps(item, default=_json_default))


def _put_profile(event: dict[str, Any], profile_id: str) -> dict[str, Any]:
    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")

    name = body.get("name")
    birth_year_month = body.get("birthYearMonth")
    if name is None and birth_year_month is None:
        return _response(400, "name and/or birthYearMonth is required")

    try:
        item = _PROFILE_TABLE.update_profile(profile_id, name=name, birth_year_month=birth_year_month)
    except ValueError as e:
        return _response(400, str(e))
    if item is None:
        return _response(404, "Profile not found")
    return _json_response(200, json.dumps(item, default=_json_default))


def _delete_profile(profile_id: str) -> dict[str, Any]:
    """Delete a profile for ever.

    If an account still names the profile as an owner, return a 409 that lists those accounts.
    """

    if _PROFILE_TABLE.get_profile(profile_id) is None:
        return _response(404, "Profile not found")

    blocking = _ACCOUNT_TABLE.accounts_with_owner(profile_id)
    blocking_sources = _INCOME_SOURCE_TABLE.sources_with_owner(profile_id)
    if blocking or blocking_sources:
        return _json_response(
            409,
            json.dumps(
                {
                    "error": "Profile is the owner of one or more accounts or income sources; reassign them first",
                    "personId": profile_id,
                    "blockingAccounts": blocking,
                    "blockingIncomeSources": blocking_sources,
                }
            ),
        )

    _PROFILE_TABLE.delete_profile(profile_id)
    return _json_response(200, json.dumps({"personId": profile_id}))


# --- Accounts (net worth) ---


def _check_owners_exist(owners: list[str]) -> dict[str, Any] | None:
    """Return a 400 response if a personId is not a person in the household, else None."""

    for owner in owners:
        if _PROFILE_TABLE.get_profile(owner) is None:
            return _response(400, f"owner '{owner}' is not a person in the household")
    return None


def _get_accounts(event: dict[str, Any]) -> dict[str, Any]:
    include_inactive = (event.get("queryStringParameters") or {}).get("include_inactive", "")
    if include_inactive.lower() == "true":
        accounts = _ACCOUNT_TABLE.get_all_accounts()
    else:
        accounts = _ACCOUNT_TABLE.get_active_accounts()
    return _json_response(200, json.dumps(accounts, default=_json_default))


def _post_account(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")

    try:
        args = parse_create_account_args(body)
    except ValueError as e:
        return _response(400, str(e))
    err = _check_owners_exist(args.owners)
    if err:
        return err

    try:
        item = _ACCOUNT_TABLE.create_account(args)
    except ValueError as e:
        return _response(400, str(e))
    return _json_response(201, json.dumps(item, default=_json_default))


def _put_account(event: dict[str, Any], account_id: str) -> dict[str, Any]:
    """Change the given fields of an account.

    See `parse_update_account_args` for the editable fields. The type of an account cannot change.
    """

    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")
    existing = _ACCOUNT_TABLE.get_account_by_id(account_id)
    if existing is None:
        return _response(404, "Account not found")

    try:
        args = parse_update_account_args(body, existing)
    except ValueError as e:
        return _response(400, str(e))
    match args.owners:
        case SetTo(value=owners):
            err = _check_owners_exist(owners)
            if err:
                return err
        case Keep():
            pass

    try:
        item = _ACCOUNT_TABLE.update_account(account_id, args)
    except ValueError as e:
        return _response(400, str(e))
    return _json_response(200, json.dumps(item, default=_json_default))


def _deactivate_account(account_id: str) -> dict[str, Any]:
    item = _ACCOUNT_TABLE.deactivate(account_id)
    if item is None:
        return _response(404, "Account not found")
    return _json_response(200, json.dumps(item, default=_json_default))


def _reactivate_account(account_id: str) -> dict[str, Any]:
    item = _ACCOUNT_TABLE.reactivate(account_id)
    if item is None:
        return _response(404, "Account not found")
    return _json_response(200, json.dumps(item, default=_json_default))


def _post_accounts_reorder(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")
    order = body.get("order")
    if not isinstance(order, list) or not all(isinstance(a, str) for a in order):
        return _response(400, "'order' must be a list of accountId strings")
    _ACCOUNT_TABLE.reorder(order)
    return _json_response(200, json.dumps(_ACCOUNT_TABLE.get_all_accounts(), default=_json_default))


# --- Net worth snapshots ---


def _reject_future_or_malformed_month(year_month: str, now: datetime) -> dict[str, Any] | None:
    """Return a 400 response if `year_month` is malformed or in the future, else None."""

    if not YEAR_MONTH_PATTERN.match(year_month):
        return _response(400, "Invalid month format, expected YYYY-MM")
    if year_month > _year_month_of(now):
        return _response(400, "Cannot record a net-worth snapshot for a future month")
    return None


def _get_networth_month(year_month: str, now: datetime) -> dict[str, Any]:
    """Return a month's snapshot rows with per-account prefill from the last prior value."""

    err = _reject_future_or_malformed_month(year_month, now)
    if err:
        return err

    accounts = _ACCOUNT_TABLE.get_all_accounts()
    month_rows = _NETWORTH_SNAPSHOT_TABLE.get_snapshot_for_month(year_month)
    prefill = compute_prefill(_NETWORTH_SNAPSHOT_TABLE.scan(), year_month)
    rows = build_month_view(accounts, month_rows, prefill)
    return _json_response(200, json.dumps({"yearMonth": year_month, "rows": rows}, default=_json_default))


def _post_networth_month(event: dict[str, Any], year_month: str, now: datetime) -> dict[str, Any]:
    """Merge the balance rows of one month.

    The body is `{rows: [{accountId, note?, classes: [{assetClass, value: number | null}, ...]}]}`. See
    `NetWorthSnapshotTable.upsert_snapshot_record` for the merge rules. An unknown accountId or asset class is rejected.
    """

    err = _reject_future_or_malformed_month(year_month, now)
    if err:
        return err

    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")
    rows = body.get("rows")
    if not isinstance(rows, list):
        return _response(400, "rows array is required")

    accounts_by_id = {a["accountId"]: a for a in _ACCOUNT_TABLE.get_all_accounts()}
    unknown: list[str] = []
    upsert_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or "accountId" not in row or "classes" not in row:
            return _response(400, "each row must have accountId and classes")
        account_id = row["accountId"]
        if account_id not in accounts_by_id:
            unknown.append(account_id)
            continue

        note = row.get("note")
        if note is not None and not isinstance(note, str):
            return _response(400, f"note for account {account_id} must be a string or null")
        if isinstance(note, str) and len(note) > 1000:
            return _response(400, f"note for account {account_id} must be 1000 characters or fewer")

        class_list = row["classes"]
        if not isinstance(class_list, list):
            return _response(400, f"classes for account {account_id} must be a list")
        classes: dict[str, Any] = {}
        for entry in class_list:
            if not isinstance(entry, dict) or "assetClass" not in entry or "value" not in entry:
                return _response(400, f"each class entry for account {account_id} must have assetClass and value")
            asset_class = entry["assetClass"]
            try:
                validate_asset_class(asset_class)
            except ValueError as e:
                return _response(400, str(e))
            value = entry["value"]
            if value is None:
                classes[asset_class] = None  # clear this class
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                return _response(400, f"value for {account_id}/{asset_class} must be an integer or null")
            if value < 0:
                return _response(400, f"value for {account_id}/{asset_class} must be >= 0")
            classes[asset_class] = value

        upsert_row: dict[str, Any] = {"accountId": account_id, "classes": classes}
        if "note" in row:
            upsert_row["note"] = note
        upsert_rows.append(upsert_row)
    if unknown:
        return _json_response(
            400,
            json.dumps({"error": "unknown accountId(s) in rows", "unknownAccountIds": sorted(set(unknown))}),
        )

    _NETWORTH_SNAPSHOT_TABLE.upsert_snapshot_record(year_month, upsert_rows)
    # Return the same per-class shape as GET so the client sees a consistent view.
    month_rows = _NETWORTH_SNAPSHOT_TABLE.get_snapshot_for_month(year_month)
    prefill = compute_prefill(_NETWORTH_SNAPSHOT_TABLE.scan(), year_month)
    view = build_month_view(list(accounts_by_id.values()), month_rows, prefill)
    return _json_response(200, json.dumps({"yearMonth": year_month, "rows": view}, default=_json_default))


def _get_networth_history() -> dict[str, Any]:
    """Return the full Excel-style history: accounts, months, values, monthly totals."""

    accounts = _ACCOUNT_TABLE.get_all_accounts()
    all_rows = _NETWORTH_SNAPSHOT_TABLE.scan()
    return _json_response(200, json.dumps(build_history(accounts, all_rows), default=_json_default))


# --- Helpers ---


def _parse_json_body(event: dict[str, Any]) -> dict[str, Any] | None:
    """Parse the JSON body of an API Gateway event. It decodes base64 when the body needs it."""

    raw_body = event.get("body", "")
    if event.get("isBase64Encoded") and raw_body:
        raw_body = base64.b64decode(raw_body).decode("utf-8")
    if not raw_body:
        return None
    return json.loads(raw_body)


def _extract_user(event: dict[str, Any]) -> dict[str, str]:
    """Return the `sub`, `email`, and `username` claims of the request, for the audit log.

    A missing claim becomes an empty string.
    """

    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    return {
        "sub": claims.get("sub", ""),
        "email": claims.get("email", ""),
        "username": claims.get("cognito:username", ""),
    }


def _resolve_override(event: dict[str, Any], body: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
    """Read the `override` flag of the body. Return `(override_active, error_response)`.

    `override_active` is True only when the body asks for an override and the user is an admin. A request for an
    override from a user who is not an admin gets a 403 response.
    """

    requested = bool(body.get("override", False))
    if not requested:
        return (False, None)
    if not is_budget_admin(event, _STAGE):
        return (False, _response(403, "Admin override requires budget-admin group membership"))
    return (True, None)


def _check_year_month_editable(year_month: str, override_active: bool, now: datetime) -> dict[str, Any] | None:
    """Return a 409 response if the year-month is locked and there is no override, else None."""

    if override_active:
        return None
    state = editability(year_month, now)
    if state == EditabilityState.LOCKED:
        return _json_response(
            409,
            json.dumps(
                {
                    "error": "year-month is locked; admin override required to mutate",
                    "yearMonth": year_month,
                    "state": EditabilityState.LOCKED,
                }
            ),
        )
    return None


def _check_year_months_editable(
    items: list[dict[str, Any]],
    year_month_key: str,
    override_active: bool,
    now: datetime,
) -> dict[str, Any] | None:
    """Batch variant. Returns 409 listing every offending item if any is in a LOCKED month."""

    if override_active:
        return None
    offending: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        ym = item.get(year_month_key)
        if not isinstance(ym, str) or not YEAR_MONTH_PATTERN.match(ym):
            continue
        if editability(ym, now) == EditabilityState.LOCKED:
            offending.append({"index": index, "yearMonth": ym})
    if offending:
        return _json_response(
            409,
            json.dumps(
                {
                    "error": "one or more items are in locked year-months; admin override required",
                    "items": offending,
                }
            ),
        )
    return None


def _json_default(obj: object) -> Any:
    """JSON serializer for types not serializable by default."""

    from decimal import Decimal

    if isinstance(obj, Decimal):
        if obj == int(obj):
            return int(obj)
        return float(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _json_response(status: int, body: str) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {**_SECURITY_HEADERS, "Content-Type": "application/json"},
        "body": body,
    }


def _response(status: int, message: str) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {**_SECURITY_HEADERS, "Content-Type": "text/plain"},
        "body": message,
    }
