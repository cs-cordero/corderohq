"""API Gateway Lambda handler for the personal finance app."""

import base64
import csv
import io
import json
import logging
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from corderohq.aws.dynamodb import (
    AccountTable,
    AuditLogTable,
    BudgetCarryStore,
    BudgetTable,
    CategoryGroupTable,
    CategoryTable,
    NetWorthSnapshotTable,
    ProfileTable,
    TransactionsTable,
    active_category_ids,
    compute_summary,
)
from corderohq.budget.auth import is_budget_admin
from corderohq.budget.csv_import import categorize_transactions, map_columns
from corderohq.budget.densify import densify, resolve_future_targets, walk_back_for
from corderohq.budget.dispositions import (
    DISPOSITION_ATTRIBUTES,
    DISPOSITION_BUDGET,
    DISPOSITION_SKIP,
    disposition_of,
    resolve_disposition_fields,
)
from corderohq.budget.editability import editability
from corderohq.networth.models import validate_asset_class
from corderohq.networth.snapshots import build_history, build_month_view, compute_prefill
from corderohq.util import YEAR_MONTH_PATTERN, get_env_var

LOGGER = logging.getLogger(__name__)

_CATEGORY_GROUP_TABLE = CategoryGroupTable(get_env_var("CATEGORY_GROUP_TABLE_NAME"))
_CATEGORY_TABLE = CategoryTable(get_env_var("CATEGORY_TABLE_NAME"))
_BUDGET_TABLE = BudgetTable(get_env_var("BUDGET_TABLE_NAME"))
_TRANSACTIONS_TABLE = TransactionsTable(get_env_var("TRANSACTIONS_TABLE_NAME"))
_AUDIT_LOG_TABLE = AuditLogTable(get_env_var("BUDGET_AUDIT_LOG_TABLE_NAME"))
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
    """Return the year-month string, in BUDGET_TZ (America/New_York).

    The budget rolls over at midnight ET on the 1st, per the architecture spec.
    Using the UTC year/month would put the rollover a few hours early for the
    east-of-UTC perspective (e.g. midnight UTC on Aug 1 is still July 31 8pm ET).
    Convert to ET first so the answer matches editability's clock.
    """
    et = now.astimezone(_BUDGET_TZ)
    return f"{et.year:04d}-{et.month:02d}"


def _resolve_now(event: dict[str, Any]) -> datetime:
    """Return the "now" that this one request must use.

    Honors `X-Simulated-Date` in non-prod stages so dev users can simulate
    viewing the app at a different date (rollover, lock, future-pin testing).
    Prod stages ignore the header entirely.

    Simulated dates in the **past** are rejected: densification cold-starts at
    the simulated year-month and would write spurious `$0` rows that linger as
    historical data once the sim is cleared. Future-only keeps the data model
    consistent with the "time only moves forward" invariant.

    Accepts ISO-8601 date (`2026-12-15`) or datetime (`2026-12-15T10:00:00Z`).
    Date-only values are anchored at noon UTC. Invalid or past values are
    logged and ignored — we fall back to real time rather than erroring the
    request.
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
    """Densify each month, up to and including the year-month of `now`.

    Idempotent and cheap when the table is already current — densify scans and
    returns 0 writes. Called at the top of every handler whose correctness
    depends on dense rows for past/current months (reads, /replace, /pin,
    category create). See docs/personal-finance-architecture.md "Densification".
    """
    densify(
        BudgetCarryStore(_BUDGET_TABLE),
        active_category_ids(_CATEGORY_TABLE.list_all()),
        _year_month_of(now),
        cold_start_value=0,
    )


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
        elif path.startswith("/api/budget/") and path.endswith("/pin") and method == "POST":
            year_month = path.split("/")[-2]
            return _post_budget_pin(event, year_month, user, now)

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

        # Summary
        elif path == "/api/summary" and method == "GET":
            return _get_summary(event, now)

        # Audit log
        elif path == "/api/audit-log" and method == "GET":
            return _get_audit_log(event)

        # Household profile (net worth)
        elif path == "/api/profile" and method == "GET":
            return _get_profile()
        elif path == "/api/profile/people" and method == "POST":
            return _post_person(event)
        elif path.startswith("/api/profile/people/") and path.endswith("/delete") and method == "POST":
            person_id = path.split("/")[-2]
            return _delete_person(person_id)
        elif path.startswith("/api/profile/people/") and method == "PUT":
            person_id = path.split("/")[-1]
            return _put_person(event, person_id)

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
    categories = _CATEGORY_TABLE.list_all() if include_inactive.lower() == "true" else _CATEGORY_TABLE.list_active()
    return _json_response(200, json.dumps(categories, default=_json_default))


def _post_category(event: dict[str, Any], now: datetime) -> dict[str, Any]:
    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")

    name = body.get("name", "")
    if not name:
        return _response(400, "name is required")

    # initialTarget is 0 by default. That is a real target, and it means "no spend this
    # month". It differs from null, which means that no one set a target. If the caller gives
    # a value, it must be an integer in millionths of a dollar, and it must not be negative.
    initial_target = body.get("initialTarget", 0)
    if not isinstance(initial_target, int) or initial_target < 0:
        return _response(400, "initialTarget must be a non-negative integer (millionths of a dollar)")

    group_id = body.get("groupId")
    if not isinstance(group_id, str) or not group_id:
        return _response(400, "groupId is required")
    if _CATEGORY_GROUP_TABLE.get(group_id) is None:
        return _response(400, f"groupId '{group_id}' does not exist")

    # Densify first. The new category then goes into a current month that is dense.
    _ensure_hydrated(now)

    try:
        item = _CATEGORY_TABLE.create(name, group_id)
    except ValueError as e:
        return _response(400, str(e))

    # A new category appears in the current month at once, so that a summary of the kind
    # Spec 1 describes shows it. A walk-back from a later month then finds initialTarget,
    # until the user pins a different amount. See "Category lifecycle" in the architecture.
    _BUDGET_TABLE.put_single(_year_month_of(now), item["categoryId"], initial_target)

    return _json_response(201, json.dumps(item, default=_json_default))


def _put_category(event: dict[str, Any], category_id: str) -> dict[str, Any]:
    """Rename a category, move it to a different group, or do both.

    Body: `{name?: string, groupId?: string}`. At least one must be present.
    `name` triggers the nameHistory append behavior (see CategoryTable.update);
    `groupId` moves the category to the new group, placing it at the end of
    that group's order.
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
            item = _CATEGORY_TABLE.update(category_id, name)
        except ValueError as e:
            return _response(400, str(e))
        if item is None:
            return _response(404, "Category not found")
    else:
        item = _CATEGORY_TABLE.get(category_id)
        if item is None:
            return _response(404, "Category not found")

    if new_group_id is not None:
        if not isinstance(new_group_id, str) or not new_group_id:
            return _response(400, "groupId must be a non-empty string")
        if _CATEGORY_GROUP_TABLE.get(new_group_id) is None:
            return _response(400, f"groupId '{new_group_id}' does not exist")
        item = _CATEGORY_TABLE.move_to_group(category_id, new_group_id)

    return _json_response(200, json.dumps(item, default=_json_default))


def _deactivate_category(
    event: dict[str, Any], category_id: str, user: dict[str, str], now: datetime
) -> dict[str, Any]:
    """Set a category to inactive. On a confirmation, delete its future pin rows.

    Two-phase: without `confirm: true` in the body, returns the list of future months
    that have pin rows for this category (so the UI can prompt the user). With
    `confirm: true`, deletes those pin rows and writes one UNPIN audit entry per drop.

    The architecture treats "deactivation itself" as not audited (no DEACTIVATE event)
    but the UNPIN entries on dropped pins ARE recorded so the budget-target log stays
    complete. See docs/personal-finance-architecture.md "Audit scope".
    """
    body = _parse_json_body(event) or {}
    confirm = bool(body.get("confirm", False))
    explanation = (body.get("explanation") or "").strip()

    if _CATEGORY_TABLE.get(category_id) is None:
        return _response(404, "Category not found")

    current_month = _year_month_of(now)
    affected_months = _BUDGET_TABLE.find_future_months_with_category(category_id, current_month)

    if affected_months and not confirm:
        # Return affectedMonths, so that the UI can show a confirmation box. Change nothing yet.
        return _json_response(
            409,
            json.dumps(
                {
                    "error": "Category has future pin rows; confirm to drop them",
                    "categoryId": category_id,
                    "affectedMonths": affected_months,
                }
            ),
        )

    if affected_months and not explanation:
        return _response(400, "explanation is required when dropping future pin rows")

    # Delete the pin rows and write the audit entries before the active flag changes. The
    # audit trail then holds the amounts that were there before.
    for month in affected_months:
        existing = _BUDGET_TABLE.get_targets(month)
        old_amount = next((t["amount"] for t in existing if t["categoryId"] == category_id), None)
        _BUDGET_TABLE.delete_single(month, category_id)
        _AUDIT_LOG_TABLE.write_entry(
            effective_year_month=month,
            category_id=category_id,
            action="UNPIN",
            explanation=explanation,
            user=user,
            changes={"amount": {"before": old_amount, "after": None}},
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
    """Answer GET /api/categories/{id}. Today only ?deletion_preview=true uses it.

    Admin-only impact summary for the hard-delete flow: counts of Budget rows,
    Transactions, and locked months affected. The locked-month count is what
    surfaces "this delete will erase data the user can't otherwise rewrite,"
    so it shows up prominently in the AdminToolsPage confirmation.
    """
    query = event.get("queryStringParameters") or {}
    if query.get("deletion_preview", "").lower() != "true":
        return _response(400, "GET /api/categories/{id} requires ?deletion_preview=true")

    if not is_budget_admin(event, _STAGE):
        return _response(403, "Admin-only endpoint")

    item = _CATEGORY_TABLE.get(category_id)
    if item is None:
        return _response(404, "Category not found")

    budget_count = _BUDGET_TABLE.count_rows_for_category(category_id)
    txn_count = _TRANSACTIONS_TABLE.count_rows_for_category(category_id)

    # A locked month is a past month with a Budget row for this category, where
    # year_month is at or before current_ym less the grace period. A month in grace is still
    # open to change, so we count it as not locked. This matches the editability function.
    current_ym = _year_month_of(now)
    past_months = _BUDGET_TABLE.find_past_months_with_category(category_id, current_ym)
    locked_months = [ym for ym in past_months if editability(ym, now) == "LOCKED"]

    return _json_response(
        200,
        json.dumps(
            {
                "category": item,
                "deletionPreview": {
                    "budgetRows": budget_count,
                    "transactions": txn_count,
                    "lockedMonthsAffected": locked_months,
                },
            },
            default=_json_default,
        ),
    )


def _hard_delete_category(event: dict[str, Any], category_id: str, user: dict[str, str]) -> dict[str, Any]:
    """Delete a category, and every row that names it, for ever.

    Admin-only via JWT group claim (no `override` flag — this is intrinsically
    admin per architecture: "CATEGORY_HARD_DELETE is the only intrinsically
    admin-only audit action"). Body requires `confirm: true`, `confirmName`
    matching the current name, and `explanation`.

    Cascade order (matters for crash recovery — if we die mid-cascade we want
    the audit entry to still be writable):
      1. Delete all Budget rows for this categoryId.
      2. Delete all Transactions for this categoryId.
      3. Delete the Category row itself.
      4. Write one CATEGORY_HARD_DELETE audit entry with counts + previous name.
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

    item = _CATEGORY_TABLE.get(category_id)
    if item is None:
        return _response(404, "Category not found")
    if confirm_name != item["name"]:
        return _response(400, f"confirmName must match the current category name exactly ('{item['name']}')")

    budget_rows_deleted = _BUDGET_TABLE.delete_all_for_category(category_id)
    txn_rows_deleted = _TRANSACTIONS_TABLE.delete_all_for_category(category_id)
    _CATEGORY_TABLE.delete(category_id)

    # Write the audit entry last. The cascade then either finished in full, or it left
    # nothing in the log to explain. effectiveYearMonth is null, as the spec states.
    _AUDIT_LOG_TABLE.write_entry(
        effective_year_month=None,
        category_id=category_id,
        action="CATEGORY_HARD_DELETE",
        explanation=explanation,
        user=user,
        changes={
            "budgetRowsDeleted": budget_rows_deleted,
            "transactionsDeleted": txn_rows_deleted,
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
                "transactionsDeleted": txn_rows_deleted,
            }
        ),
    )


# --- Category groups ---


def _get_category_groups() -> dict[str, Any]:
    return _json_response(200, json.dumps(_CATEGORY_GROUP_TABLE.list_all(), default=_json_default))


def _post_category_group(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")
    name = body.get("name", "")
    if not name:
        return _response(400, "name is required")
    try:
        item = _CATEGORY_GROUP_TABLE.create(name)
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
    existing = _CATEGORY_GROUP_TABLE.get(group_id)
    if existing is None:
        return _response(404, "Group not found")
    if existing.get("system"):
        return _response(403, "System groups cannot be renamed")
    try:
        item = _CATEGORY_GROUP_TABLE.update(group_id, name)
    except ValueError as e:
        return _response(400, str(e))
    return _json_response(200, json.dumps(item, default=_json_default))


def _delete_category_group(group_id: str) -> dict[str, Any]:
    """Hard-delete a group.

    Two rules:
    - System groups (Unassigned) cannot be deleted.
    - Active categories block the delete with a 409 — the user must move them
      to another group first.

    Deactivated categories in the group are auto-moved to the Unassigned
    sentinel group on delete so they don't leave a dangling groupId reference.
    """
    existing = _CATEGORY_GROUP_TABLE.get(group_id)
    if existing is None:
        return _response(404, "Group not found")
    if existing.get("system"):
        return _response(403, "System groups cannot be deleted")

    cats_in_group = [c for c in _CATEGORY_TABLE.list_all() if c.get("groupId") == group_id]
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

    _CATEGORY_GROUP_TABLE.delete(group_id)
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
    return _json_response(200, json.dumps(_CATEGORY_GROUP_TABLE.list_all(), default=_json_default))


def _post_categories_reorder(event: dict[str, Any]) -> dict[str, Any]:
    """Apply a new ordering for categories within a group.

    Body: `{groupId, order: [categoryId, ...]}`. Each category in `order` is set
    to `(groupId, orderInGroup=index)`. Effectively also supports cross-group
    moves: pass a category currently in group X with target groupId Y and it
    migrates as part of the reorder.
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
    if _CATEGORY_GROUP_TABLE.get(group_id) is None:
        return _response(404, "Group not found")
    _CATEGORY_TABLE.reorder_in_group(group_id, order)
    return _json_response(200, json.dumps(_CATEGORY_TABLE.list_all(), default=_json_default))


# --- Budget ---


def _get_budget(year_month: str, now: datetime) -> dict[str, Any]:
    if not YEAR_MONTH_PATTERN.match(year_month):
        return _response(400, "Invalid month format, expected YYYY-MM")

    _ensure_hydrated(now)

    current_ym = _year_month_of(now)
    if year_month <= current_ym:
        # Dense month (past, grace, current). Stored rows are authoritative;
        # no walk-back needed.
        return _json_response(200, json.dumps(_BUDGET_TABLE.get_targets(year_month), default=_json_default))

    # Future month: resolve effective values via per-category walk-back,
    # attaching a `pinned` flag so the UI can distinguish explicit pins from
    # carried values.
    targets = resolve_future_targets(
        BudgetCarryStore(_BUDGET_TABLE), active_category_ids(_CATEGORY_TABLE.list_all()), year_month
    )
    return _json_response(200, json.dumps(targets, default=_json_default))


def _post_budget_replace(event: dict[str, Any], year_month: str, user: dict[str, str], now: datetime) -> dict[str, Any]:
    """Replace-all write for a dense month (current, grace, or locked-via-override).

    Architecture invariants enforced here:
    - Body must include every category that has an existing row OR a non-zero transaction
      in this month. Missing categories return a single 409 listing all of them.
    - Future months are rejected — they're sparse and go through /pin instead.
    - Audit emits CREATE (new row) or UPDATE (changed amount) per category. /replace never
      produces DELETE entries; row deletions happen via /pin (null), deactivation drop, or
      hard delete.
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

    for target in targets:
        if "categoryId" not in target or "amount" not in target:
            return _response(400, "Each target must have categoryId and amount")

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
                    "error": "/replace is for dense months only; use /pin for future months",
                    "yearMonth": year_month,
                }
            ),
        )

    existing = _BUDGET_TABLE.get_targets(year_month)
    existing_cat_ids = {t["categoryId"] for t in existing}
    txns = _TRANSACTIONS_TABLE.list_for_month(year_month)
    txn_cat_ids = {t["categoryId"] for t in txns if disposition_of(t) == DISPOSITION_BUDGET and t.get("amount") != 0}
    required_cat_ids = existing_cat_ids | txn_cat_ids

    body_cat_ids = {t["categoryId"] for t in targets}
    missing = sorted(required_cat_ids - body_cat_ids)
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

    old_by_category = {t["categoryId"]: t["amount"] for t in existing}
    items = _BUDGET_TABLE.put_targets(year_month, targets)

    new_by_category = {t["categoryId"]: t["amount"] for t in items}
    for cat_id, new_amount in new_by_category.items():
        old_amount = old_by_category.get(cat_id)
        if old_amount == new_amount:
            continue

        if old_amount is None:
            action = "CREATE"
            changes: dict[str, Any] = {"amount": {"before": None, "after": new_amount}}
        else:
            action = "UPDATE"
            changes = {"amount": {"before": old_amount, "after": new_amount}}

        _AUDIT_LOG_TABLE.write_entry(
            effective_year_month=year_month,
            category_id=cat_id,
            action=action,
            explanation=explanation,
            user=user,
            changes=changes,
            override=override_active,
        )

    return _json_response(200, json.dumps(items, default=_json_default))


def _post_budget_pin(event: dict[str, Any], year_month: str, user: dict[str, str], now: datetime) -> dict[str, Any]:
    """Pin / unpin future-month rows.

    Editability: `year_month` must be EDITABLE AND strictly in the future.
    Current / grace / locked months reject — those are dense and /replace is the
    right endpoint. Override has no effect here (architecture: "Override does not
    unlock /pin for non-future months").

    Body: `{targets: [{categoryId, amount: number | null}], explanation}`. Upsert
    semantics — categories omitted from `targets` are untouched. `amount: null`
    deletes the pin row (UNPIN audit entry); a number creates or updates the pin
    row (PIN audit entry). Same-value pins are no-ops with no audit.

    Returns `{targets: <walk-back-resolved rows for year_month>, warnings:
    {downstreamPins, pinMatchesCarriedValue}}`.
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

    for target in targets:
        if "categoryId" not in target or "amount" not in target:
            return _response(400, "Each target must have categoryId and amount (amount may be null)")
        if target["amount"] is not None and not isinstance(target["amount"], int):
            return _response(400, "amount must be an integer or null")

    current_ym = _year_month_of(now)
    if year_month <= current_ym:
        return _json_response(
            409,
            json.dumps(
                {
                    "error": "/pin is for future months only; use /replace for current/grace/locked months",
                    "yearMonth": year_month,
                }
            ),
        )
    state = editability(year_month, now)
    if state != "EDITABLE":
        return _json_response(
            409,
            json.dumps(
                {
                    "error": f"/pin requires an editable future month; year-month is {state}",
                    "yearMonth": year_month,
                    "state": state,
                }
            ),
        )

    _ensure_hydrated(now)

    # Snapshot table state before any writes so walk-back warnings are computed
    # against the pre-write world.
    rows_before = _BUDGET_TABLE.scan_all()
    by_key_before: dict[tuple[str, str], Any] = {(r["yearMonth"], r["categoryId"]): r["amount"] for r in rows_before}

    pin_matches_carried: list[str] = []

    for target in targets:
        cat_id = target["categoryId"]
        new_amount = target["amount"]
        old_amount = by_key_before.get((year_month, cat_id))

        if new_amount is None:
            if old_amount is None:
                continue
            _BUDGET_TABLE.delete_single(year_month, cat_id)
            _AUDIT_LOG_TABLE.write_entry(
                effective_year_month=year_month,
                category_id=cat_id,
                action="UNPIN",
                explanation=explanation,
                user=user,
                changes={"amount": {"before": old_amount, "after": None}},
            )
        else:
            if old_amount == new_amount:
                continue
            _BUDGET_TABLE.put_single(year_month, cat_id, new_amount)
            # Warning: would walk-back have resolved to this same value without the pin?
            sans_pin = {k: v for k, v in by_key_before.items() if k != (year_month, cat_id)}
            if walk_back_for(sans_pin, year_month, cat_id) == new_amount:
                pin_matches_carried.append(cat_id)
            _AUDIT_LOG_TABLE.write_entry(
                effective_year_month=year_month,
                category_id=cat_id,
                action="PIN",
                explanation=explanation,
                user=user,
                changes={"amount": {"before": old_amount, "after": new_amount}},
            )

    # Re-scan for fresh state, then compute downstream pins + resolved view.
    rows_after = _BUDGET_TABLE.scan_all()
    touched_cat_ids = {t["categoryId"] for t in targets}
    downstream_pins = sorted(
        (
            {"categoryId": r["categoryId"], "yearMonth": r["yearMonth"]}
            for r in rows_after
            if r["categoryId"] in touched_cat_ids and r["yearMonth"] > year_month
        ),
        key=lambda p: (p["yearMonth"], p["categoryId"]),
    )

    resolved = resolve_future_targets(
        BudgetCarryStore(_BUDGET_TABLE), active_category_ids(_CATEGORY_TABLE.list_all()), year_month
    )

    return _json_response(
        200,
        json.dumps(
            {
                "targets": resolved,
                "warnings": {
                    "downstreamPins": downstream_pins,
                    "pinMatchesCarriedValue": sorted(pin_matches_carried),
                },
            },
            default=_json_default,
        ),
    )


# --- Transactions ---


def _get_transactions(event: dict[str, Any]) -> dict[str, Any]:
    month = (event.get("queryStringParameters") or {}).get("month", "")
    if not month or not YEAR_MONTH_PATTERN.match(month):
        return _response(400, "query parameter 'month' is required (YYYY-MM)")

    txns = _TRANSACTIONS_TABLE.list_for_month(month)
    return _json_response(200, json.dumps(txns, default=_json_default))


def _check_account_exists(account_id: str | None) -> dict[str, Any] | None:
    """Return a 400 response if `account_id` is set and names no account. Else return None."""
    if account_id is not None and _ACCOUNT_TABLE.get(account_id) is None:
        return _response(400, f"accountId '{account_id}' does not exist")
    return None


def _post_transaction(event: dict[str, Any], now: datetime) -> dict[str, Any]:
    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")

    required = ("yearMonth", "transactionDate", "description", "amount")
    for field in required:
        if field not in body:
            return _response(400, f"{field} is required")

    try:
        fields = resolve_disposition_fields(body)
    except ValueError as e:
        return _response(400, str(e))
    err = _check_account_exists(fields.account_id)
    if err:
        return err

    override_active, err = _resolve_override(event, body)
    if err:
        return err
    err = _check_year_month_editable(body["yearMonth"], override_active, now)
    if err:
        return err

    try:
        item = _TRANSACTIONS_TABLE.create(
            year_month=body["yearMonth"],
            transaction_date=body["transactionDate"],
            description=body["description"],
            amount=body["amount"],
            category_id=fields.category_id,
            disposition=fields.disposition,
            account_id=fields.account_id,
            note=fields.note,
        )
    except ValueError as e:
        return _response(400, str(e))

    return _json_response(201, json.dumps(item, default=_json_default))


def _validate_commit_rows(
    rows: list[dict[str, Any]],
    valid_category_ids: set[str],
    override_active: bool,
    now: datetime,
) -> list[dict[str, Any]]:
    """Per-row validation for /transactions/commit and /transactions/upload.

    Returns one validation block per input row:
        {"index": i, "issues": ["missing_category"?, "locked_month"?]}

    `missing_category` applies to a BUDGET row with no known category, and to an OFF_BUDGET row
    with a category that does not exist. A row with no disposition is a BUDGET row.

    Issues use a closed enum so the frontend can render specific UI per issue.
    `locked_month` is suppressed when `override_active=True` since admin override
    intentionally bypasses the editability gate.
    """
    results: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        issues: list[str] = []
        cat_id = row.get("categoryId")
        disposition = disposition_of(row)
        category_unknown = bool(cat_id) and cat_id not in valid_category_ids
        if (disposition == DISPOSITION_BUDGET and not cat_id) or (disposition != DISPOSITION_SKIP and category_unknown):
            issues.append("missing_category")
        txn_date = row.get("transactionDate") or ""
        ym = txn_date[:7] if len(txn_date) >= 7 else ""
        if ym and YEAR_MONTH_PATTERN.match(ym):
            state = editability(ym, now)
            if state == "LOCKED" and not override_active:
                issues.append("locked_month")
        results.append({"index": i, "issues": issues})
    return results


def _post_transactions_commit(event: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Atomic, all-or-nothing CSV commit.

    Body: `{rows: [{transactionDate, description, amount, categoryId?, disposition?, accountId?,
    note?}, ...], override?}`. `categoryId` is necessary only for a BUDGET row.
    Pre-flight validates every row (categoryId present and known; transactionDate
    year-month is editable or admin-overridden). If any row has issues, returns 409
    with the full validation list so the UI can render every offender in one pass —
    no partial commits.

    On success, batch-writes all rows. The handler is intentionally NOT idempotent:
    re-running on the same body will create duplicate transactions because each
    DynamoDB item gets a fresh ULID-suffixed sort key. Callers should treat a 200
    as final and a non-200 as "nothing happened" — never a "retry safely" signal.
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

    required_fields = ("transactionDate", "description", "amount")
    checked_rows: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        for field in required_fields:
            if field not in row:
                return _response(400, f"row {i}: missing required field '{field}'")
        if not isinstance(row["amount"], int):
            return _response(400, f"row {i}: amount must be an integer (millionths of a dollar)")
        try:
            # A missing category is a row issue (see _validate_commit_rows), not an error.
            fields = resolve_disposition_fields(row, require_category=False)
        except ValueError as e:
            return _response(400, f"row {i}: {e}")
        err = _check_account_exists(fields.account_id)
        if err:
            return err
        checked = {key: value for key, value in row.items() if key not in DISPOSITION_ATTRIBUTES}
        checked.update(fields.stored_values())
        checked_rows.append(checked)

    _ensure_hydrated(now)

    valid_category_ids = {c["categoryId"] for c in _CATEGORY_TABLE.list_all()}
    validations = _validate_commit_rows(checked_rows, valid_category_ids, override_active, now)
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

    items_to_create: list[dict[str, Any]] = []
    for row in checked_rows:
        item: dict[str, Any] = {
            "yearMonth": str(row["transactionDate"])[:7],
            "transactionDate": row["transactionDate"],
            "description": row["description"],
            "amount": row["amount"],
        }
        item.update({name: row[name] for name in DISPOSITION_ATTRIBUTES if name in row})
        items_to_create.append(item)
    created = _TRANSACTIONS_TABLE.batch_create(items_to_create)

    return _json_response(200, json.dumps({"count": len(created), "transactions": created}, default=_json_default))


def _post_transactions_upload(event: dict[str, Any], now: datetime) -> dict[str, Any]:
    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")

    csv_data = body.get("csvData", "")
    if not csv_data:
        return _response(400, "csvData is required")

    # Parse CSV
    reader = csv.reader(io.StringIO(csv_data))
    rows = list(reader)
    if len(rows) < 2:
        return _response(400, "CSV must have a header row and at least one data row")

    headers = rows[0]
    data_rows = rows[1:]

    # Step 1: the LLM finds which column is which.
    sample_rows = data_rows[:5]
    column_mapping = map_columns(headers, sample_rows)

    date_col = column_mapping.get("date", "")
    desc_col = column_mapping.get("description", "")
    amount_col = column_mapping.get("amount", "")
    amount_invert = column_mapping.get("amount_invert", False)

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

    parsed_transactions: list[dict[str, Any]] = []
    descriptions: list[str] = []
    for row in data_rows:
        if len(row) <= max(date_idx, desc_idx, amount_idx):
            continue
        raw_amount = row[amount_idx].strip().replace(",", "").replace("$", "")
        if not raw_amount:
            continue
        try:
            amount_float = float(raw_amount)
        except ValueError:
            continue

        # Our schema is positive=expense, negative=income/return. If the bank CSV
        # uses the opposite (negative=debit), the LLM column-mapper sets
        # amount_invert and we flip every row to land on our convention.
        if amount_invert:
            amount_float = -amount_float

        amount_millionths = round(amount_float * 1_000_000)

        parsed_transactions.append(
            {
                "transactionDate": row[date_idx].strip(),
                "description": row[desc_idx].strip(),
                "amount": amount_millionths,
            }
        )
        descriptions.append(row[desc_idx].strip())

    if not parsed_transactions:
        return _response(400, "No valid transactions found in CSV")

    # Step 3: the LLM gives a category to each row.
    categories = _CATEGORY_TABLE.list_active()
    if categories:
        assignments = categorize_transactions(descriptions, categories)
        for i, assignment in enumerate(assignments):
            if i < len(parsed_transactions):
                parsed_transactions[i]["categoryId"] = assignment.get("categoryId", "")
                parsed_transactions[i]["categoryName"] = assignment.get("categoryName", "")
    else:
        for txn in parsed_transactions:
            txn["categoryId"] = ""
            txn["categoryName"] = ""

    # Per-row pre-validation surfaces missing_category / locked_month issues so the
    # review UI can render specific annotations without round-tripping through
    # /commit just to learn what's wrong.
    valid_category_ids = {c["categoryId"] for c in _CATEGORY_TABLE.list_all()}
    validations = _validate_commit_rows(parsed_transactions, valid_category_ids, override_active=False, now=now)

    return _json_response(
        200,
        json.dumps(
            {
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

    updates: dict[str, Any] = {}
    for field in ("description", "amount", *DISPOSITION_ATTRIBUTES):
        if field in body:
            updates[field] = body[field]

    account_id = updates.get("accountId")
    if isinstance(account_id, str) and account_id.strip():
        err = _check_account_exists(account_id.strip())
        if err:
            return err

    try:
        item = _TRANSACTIONS_TABLE.update(year_month, sort_id, updates)
    except ValueError as e:
        return _response(400, str(e))
    if item is None:
        return _response(404, "Transaction not found")

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

    deleted = _TRANSACTIONS_TABLE.batch_delete(items)
    return _json_response(200, json.dumps({"deleted": deleted}))


# --- Summary ---


def _get_summary(event: dict[str, Any], now: datetime) -> dict[str, Any]:
    month = (event.get("queryStringParameters") or {}).get("month", "")
    if not month or not YEAR_MONTH_PATTERN.match(month):
        return _response(400, "query parameter 'month' is required (YYYY-MM)")

    _ensure_hydrated(now)

    result = compute_summary(_BUDGET_TABLE, _TRANSACTIONS_TABLE, _CATEGORY_TABLE, month, now)
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


def _get_profile() -> dict[str, Any]:
    return _json_response(200, json.dumps(_PROFILE_TABLE.list_people(), default=_json_default))


def _post_person(event: dict[str, Any]) -> dict[str, Any]:
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
        item = _PROFILE_TABLE.create_person(name, birth_year_month)
    except ValueError as e:
        return _response(400, str(e))
    return _json_response(201, json.dumps(item, default=_json_default))


def _put_person(event: dict[str, Any], person_id: str) -> dict[str, Any]:
    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")

    name = body.get("name")
    birth_year_month = body.get("birthYearMonth")
    if name is None and birth_year_month is None:
        return _response(400, "name and/or birthYearMonth is required")

    try:
        item = _PROFILE_TABLE.update_person(person_id, name=name, birth_year_month=birth_year_month)
    except ValueError as e:
        return _response(400, str(e))
    if item is None:
        return _response(404, "Person not found")
    return _json_response(200, json.dumps(item, default=_json_default))


def _delete_person(person_id: str) -> dict[str, Any]:
    """Hard-delete a person, blocked if any account still names them as owner.

    People are rarely removed — this endpoint exists to undo a fat-fingered create,
    so a 409 listing the referencing accounts (rather than cascading) keeps owner
    references from silently dangling.
    """
    if _PROFILE_TABLE.get_person(person_id) is None:
        return _response(404, "Person not found")

    blocking = _ACCOUNT_TABLE.accounts_with_owner(person_id)
    if blocking:
        return _json_response(
            409,
            json.dumps(
                {
                    "error": "Person is the owner of one or more accounts; reassign them first",
                    "personId": person_id,
                    "blockingAccounts": blocking,
                }
            ),
        )

    _PROFILE_TABLE.delete_person(person_id)
    return _json_response(200, json.dumps({"personId": person_id}))


# --- Accounts (net worth) ---


def _validate_owners(owners: Any) -> tuple[list[str] | None, dict[str, Any] | None]:
    """Validate an account's `owners` list, returning (normalized_owners, error).

    An account is owned by one or more household people. Valid input is a non-empty
    list of personId strings that each exist in the household. On success returns
    (deduped list, None); on failure (None, 400 response). The foreign-key existence
    check is the part the table wrapper deliberately leaves to the handler (it needs
    a Profile lookup).
    """
    if not isinstance(owners, list) or not owners:
        return None, _response(400, "owners must be a non-empty list of personIds")
    normalized: list[str] = []
    seen: set[str] = set()
    for owner in owners:
        if not isinstance(owner, str) or not owner:
            return None, _response(400, "each owner must be a personId string")
        if _PROFILE_TABLE.get_person(owner) is None:
            return None, _response(400, f"owner '{owner}' is not a person in the household")
        if owner not in seen:
            seen.add(owner)
            normalized.append(owner)
    return normalized, None


def _get_accounts(event: dict[str, Any]) -> dict[str, Any]:
    include_inactive = (event.get("queryStringParameters") or {}).get("include_inactive", "")
    accounts = _ACCOUNT_TABLE.list_all() if include_inactive.lower() == "true" else _ACCOUNT_TABLE.list_active()
    return _json_response(200, json.dumps(accounts, default=_json_default))


def _post_account(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")

    # `liability` and (for fixed-class types) `assetClasses` are derived from the type
    # server-side, so they are not required inputs — only name, type, and owners are.
    for field in ("name", "accountType"):
        if not body.get(field):
            return _response(400, f"{field} is required")

    owners, err = _validate_owners(body.get("owners"))
    if err:
        return err
    if owners is None:
        # _validate_owners returns a list whenever it returns no error. This check tells the type
        # checker so.
        return _response(400, "owners is required")

    try:
        item = _ACCOUNT_TABLE.create(
            name=body["name"],
            account_type=body["accountType"],
            asset_classes=body.get("assetClasses"),
            owners=owners,
            excluded_from_net_worth=bool(body.get("excludedFromNetWorth", False)),
            target_year=body.get("targetYear"),
            loan_terms=body.get("loanTerms"),
            notes=body.get("notes"),
        )
    except ValueError as e:
        return _response(400, str(e))
    return _json_response(201, json.dumps(item, default=_json_default))


def _put_account(event: dict[str, Any], account_id: str) -> dict[str, Any]:
    """Edit an account's mutable fields.

    Body may carry any of name/assetClasses/owners/excludedFromNetWorth/targetYear/
    loanTerms/notes/sortOrder. A field present with value null removes the optional
    attribute (loanTerms/notes). `owners` and `assetClasses`, when present, are
    non-empty lists that replace the current values. `targetYear` is reconciled
    against the effective asset-class set. `accountType` and `liability` are immutable
    (the type drives liability, the asset-class rules, and loan-term eligibility) and
    are ignored here.
    """
    body = _parse_json_body(event)
    if body is None:
        return _response(400, "Request body is required")

    editable_fields = (
        "name",
        "assetClasses",
        "owners",
        "excludedFromNetWorth",
        "targetYear",
        "loanTerms",
        "notes",
        "sortOrder",
    )
    updates = {field: body[field] for field in editable_fields if field in body}
    if not updates:
        return _response(400, f"at least one editable field is required: {list(editable_fields)}")

    if "owners" in updates:
        owners, err = _validate_owners(updates["owners"])
        if err:
            return err
        updates["owners"] = owners

    try:
        item = _ACCOUNT_TABLE.update(account_id, updates)
    except ValueError as e:
        return _response(400, str(e))
    if item is None:
        return _response(404, "Account not found")
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
    return _json_response(200, json.dumps(_ACCOUNT_TABLE.list_all(), default=_json_default))


# --- Net worth snapshots ---


def _reject_future_or_malformed_month(year_month: str, now: datetime) -> dict[str, Any] | None:
    """Return a 400 response if `year_month` is malformed or in the future, else None.

    Recording a future balance is meaningless (that's the simulator's job someday),
    so the effective-now check reuses the same clock as the budget feature, keeping
    the dev X-Simulated-Date header working.
    """
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

    accounts = _ACCOUNT_TABLE.list_all()
    month_rows = _NETWORTH_SNAPSHOT_TABLE.get_month(year_month)
    prefill = compute_prefill(_NETWORTH_SNAPSHOT_TABLE.scan_all(), year_month)
    rows = build_month_view(accounts, month_rows, prefill)
    return _json_response(200, json.dumps({"yearMonth": year_month, "rows": rows}, default=_json_default))


def _post_networth_month(event: dict[str, Any], year_month: str, now: datetime) -> dict[str, Any]:
    """Upsert a month's snapshot rows, per asset class.

    Body: {rows: [{accountId, note?: string | null,
                   classes: [{assetClass, value: number | null}, ...]}]}. Per class, a
    non-negative integer (millionths) writes that class's value; null clears just that
    class; classes omitted from the list are left untouched (per-class merge). A `note`
    key replaces/clears the account-month note; omitting it preserves the note.
    Accounts omitted from `rows` are untouched. Unknown accountIds and asset-class
    values are rejected.
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

    accounts_by_id = {a["accountId"]: a for a in _ACCOUNT_TABLE.list_all()}
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

    _NETWORTH_SNAPSHOT_TABLE.upsert_month(year_month, upsert_rows)
    # Return the same per-class shape as GET so the client sees a consistent view.
    month_rows = _NETWORTH_SNAPSHOT_TABLE.get_month(year_month)
    prefill = compute_prefill(_NETWORTH_SNAPSHOT_TABLE.scan_all(), year_month)
    view = build_month_view(list(accounts_by_id.values()), month_rows, prefill)
    return _json_response(200, json.dumps({"yearMonth": year_month, "rows": view}, default=_json_default))


def _get_networth_history() -> dict[str, Any]:
    """Return the full Excel-style history: accounts, months, values, monthly totals."""
    accounts = _ACCOUNT_TABLE.list_all()
    all_rows = _NETWORTH_SNAPSHOT_TABLE.scan_all()
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
    """Snapshot the JWT-claims user identity for the audit log.

    Reads `sub`, `email`, `cognito:username` from the API Gateway JWT-authorizer claims.
    Per the architecture doc's "Audit user identity" section, these are written verbatim
    into audit entries as a point-in-time snapshot; subsequent profile edits don't rewrite
    history. Missing claims fall back to empty strings rather than raising — handlers that
    don't end up writing audit entries shouldn't fail on a malformed token here.
    """
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    return {
        "sub": claims.get("sub", ""),
        "email": claims.get("email", ""),
        "username": claims.get("cognito:username", ""),
    }


def _resolve_override(event: dict[str, Any], body: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
    """Parse `override` flag from body and verify the JWT carries admin claim.

    Returns (override_active, error_response).
    - override_active is True only if override was requested AND the JWT carries the admin group claim.
    - error_response is non-None when override was requested but the JWT lacks the admin group claim (403).
    """
    requested = bool(body.get("override", False))
    if not requested:
        return (False, None)
    if not is_budget_admin(event, _STAGE):
        return (False, _response(403, "Admin override requires budget-admin group membership"))
    return (True, None)


def _check_year_month_editable(year_month: str, override_active: bool, now: datetime) -> dict[str, Any] | None:
    """Return a 409 response dict if the year-month is locked, else None.

    `override_active` skips the check entirely.
    """
    if override_active:
        return None
    state = editability(year_month, now)
    if state == "LOCKED":
        return _json_response(
            409,
            json.dumps(
                {
                    "error": "year-month is locked; admin override required to mutate",
                    "yearMonth": year_month,
                    "state": "LOCKED",
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
        if editability(ym, now) == "LOCKED":
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
