import json
import os
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("CATEGORY_GROUP_TABLE_NAME", "test-CategoryGroup")
os.environ.setdefault("CATEGORY_TABLE_NAME", "test-Category")
os.environ.setdefault("BUDGET_TABLE_NAME", "test-Budget")
os.environ.setdefault("BUDGET_AUDIT_LOG_TABLE_NAME", "test-BudgetAuditLog")
os.environ.setdefault("TRANSACTIONS_TABLE_NAME", "test-Transactions")
os.environ.setdefault("PROFILE_TABLE_NAME", "test-Profile")
os.environ.setdefault("ACCOUNT_TABLE_NAME", "test-Account")
os.environ.setdefault("NETWORTH_SNAPSHOT_TABLE_NAME", "test-NetWorthSnapshot")
os.environ.setdefault("SSM_API_KEY_PARAM", "/budget/anthropic-api-key")
os.environ.setdefault("STAGE", "test")

from corderohq.accounts import AccountType, CreateAccountArgs, FixedClassAccount
from corderohq.personal_finance_handler import handler

# Stable "now" used across editability-sensitive tests: June 15, 2026 noon UTC.
# Relative to this:
#   - "2026-06" is current → EDITABLE
#   - "2026-07" .. "2027-06" are next 12 months → EDITABLE
#   - "2026-05" is the previous month and grace ended on 2026-06-08 → LOCKED
#   - "2026-03", "2026-04" → LOCKED (older)
_FROZEN_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)

_EDITABLE_MONTH = "2026-06"
_LOCKED_MONTH = "2026-03"


@pytest.fixture(autouse=True)
def _freeze_now() -> "object":
    """Freeze `_now_utc` for every test so editability is deterministic."""

    with patch("corderohq.personal_finance_handler._now_utc", return_value=_FROZEN_NOW) as p:
        yield p


@pytest.fixture(autouse=True)
def _skip_hydration() -> "object":
    """Default lazy-hydration to a no-op for handler tests.

    Densification has its own test module (test_budget_densify.py); most handler
    tests don't care about densify writes and would otherwise need to mock
    `_BUDGET_TABLE.scan_all` + `_CATEGORY_TABLE.get_all_categories` everywhere. Tests that
    DO want to exercise the hydration path can re-patch this within their scope.
    """

    with patch("corderohq.personal_finance_handler._ensure_hydrated") as p:
        yield p


def _make_event(
    method: str,
    path: str,
    body: dict | None = None,
    query: dict | None = None,
    groups: str | list[str] | None = None,
) -> dict:
    claims: dict[str, object] = {"email": "test@example.com"}
    if groups is not None:
        claims["cognito:groups"] = groups
    event: dict = {
        "requestContext": {
            "http": {"method": method},
            "authorizer": {"jwt": {"claims": claims}},
        },
        "rawPath": path,
    }
    if body is not None:
        event["body"] = json.dumps(body)
    if query is not None:
        event["queryStringParameters"] = query
    return event


class TestGetCategories:
    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    def test_returns_categories(self, mock_ct: MagicMock) -> None:
        mock_ct.get_active_categories.return_value = [{"categoryId": "cat1", "name": "Groceries", "active": True}]
        result = handler(_make_event("GET", "/api/categories"), None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body) == 1
        assert body[0]["name"] == "Groceries"


class TestPostCategory:
    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_GROUP_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    def test_creates_category_with_initial_target(
        self, mock_ct: MagicMock, mock_cg: MagicMock, mock_bt: MagicMock
    ) -> None:
        mock_cg.get_category_group_by_id.return_value = {"groupId": "g1", "name": "Essentials"}
        mock_ct.create_category.return_value = {
            "categoryId": "cat1",
            "name": "Groceries",
            "active": True,
            "nameHistory": [],
            "groupId": "g1",
            "orderInGroup": 0,
        }

        result = handler(
            _make_event(
                "POST",
                "/api/categories",
                body={"name": "Groceries", "initialTarget": 50_000_000, "groupId": "g1"},
            ),
            None,
        )

        assert result["statusCode"] == 201
        mock_ct.create_category.assert_called_once_with("Groceries", "g1")
        mock_bt.put_single.assert_called_once_with(_EDITABLE_MONTH, "cat1", 50_000_000)

    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_GROUP_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    def test_initial_target_defaults_to_zero_when_omitted(
        self, mock_ct: MagicMock, mock_cg: MagicMock, mock_bt: MagicMock
    ) -> None:
        # Spec 14: initialTarget is optional and defaults to $0.
        mock_cg.get_category_group_by_id.return_value = {"groupId": "g1", "name": "Essentials"}
        mock_ct.create_category.return_value = {
            "categoryId": "cat1",
            "name": "Groceries",
            "active": True,
            "nameHistory": [],
            "groupId": "g1",
            "orderInGroup": 0,
        }
        result = handler(
            _make_event("POST", "/api/categories", body={"name": "Groceries", "groupId": "g1"}),
            None,
        )
        assert result["statusCode"] == 201
        mock_bt.put_single.assert_called_once_with(_EDITABLE_MONTH, "cat1", 0)

    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_GROUP_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    def test_initial_target_zero_is_accepted(self, mock_ct: MagicMock, mock_cg: MagicMock, mock_bt: MagicMock) -> None:
        mock_cg.get_category_group_by_id.return_value = {"groupId": "g1", "name": "Essentials"}
        mock_ct.create_category.return_value = {
            "categoryId": "cat1",
            "name": "Groceries",
            "active": True,
            "nameHistory": [],
            "groupId": "g1",
            "orderInGroup": 0,
        }
        result = handler(
            _make_event(
                "POST",
                "/api/categories",
                body={"name": "Groceries", "initialTarget": 0, "groupId": "g1"},
            ),
            None,
        )
        assert result["statusCode"] == 201

    def test_returns_400_when_initial_target_negative(self) -> None:
        result = handler(
            _make_event(
                "POST",
                "/api/categories",
                body={"name": "Groceries", "initialTarget": -100, "groupId": "g1"},
            ),
            None,
        )
        assert result["statusCode"] == 400

    def test_returns_400_when_name_missing(self) -> None:
        result = handler(
            _make_event("POST", "/api/categories", body={"initialTarget": 100, "groupId": "g1"}),
            None,
        )
        assert result["statusCode"] == 400

    def test_returns_400_when_group_id_missing(self) -> None:
        result = handler(
            _make_event("POST", "/api/categories", body={"name": "Groceries", "initialTarget": 50}),
            None,
        )
        assert result["statusCode"] == 400
        assert "groupId" in result["body"]

    @patch("corderohq.personal_finance_handler._CATEGORY_GROUP_TABLE")
    def test_returns_400_when_group_id_unknown(self, mock_cg: MagicMock) -> None:
        mock_cg.get_category_group_by_id.return_value = None
        result = handler(
            _make_event(
                "POST",
                "/api/categories",
                body={"name": "Groceries", "initialTarget": 50, "groupId": "unknown"},
            ),
            None,
        )
        assert result["statusCode"] == 400


class TestPutCategory:
    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    def test_updates_category_name(self, mock_ct: MagicMock) -> None:
        mock_ct.update.return_value = {
            "categoryId": "cat1",
            "name": "New Name",
            "active": True,
            "nameHistory": [],
            "groupId": "g1",
            "orderInGroup": 0,
        }
        result = handler(_make_event("PUT", "/api/categories/cat1", body={"name": "New Name"}), None)
        assert result["statusCode"] == 200

    @patch("corderohq.personal_finance_handler._CATEGORY_GROUP_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    def test_moves_category_to_new_group(self, mock_ct: MagicMock, mock_cg: MagicMock) -> None:
        mock_cg.get_category_group_by_id.return_value = {"groupId": "g2", "name": "Discretionary"}
        mock_ct.get_category_by_id.return_value = {"categoryId": "cat1", "name": "Coffee", "groupId": "g1"}
        mock_ct.move_to_group.return_value = {
            "categoryId": "cat1",
            "name": "Coffee",
            "groupId": "g2",
            "orderInGroup": 0,
        }
        result = handler(_make_event("PUT", "/api/categories/cat1", body={"groupId": "g2"}), None)
        assert result["statusCode"] == 200
        mock_ct.move_to_group.assert_called_once_with("cat1", "g2")

    def test_returns_400_when_neither_name_nor_group_id_given(self) -> None:
        result = handler(_make_event("PUT", "/api/categories/cat1", body={}), None)
        assert result["statusCode"] == 400

    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    def test_returns_404_when_not_found(self, mock_ct: MagicMock) -> None:
        mock_ct.update.return_value = None
        result = handler(_make_event("PUT", "/api/categories/cat1", body={"name": "New"}), None)
        assert result["statusCode"] == 404


class TestCategoryGroupEndpoints:
    @patch("corderohq.personal_finance_handler._CATEGORY_GROUP_TABLE")
    def test_list_groups(self, mock_cg: MagicMock) -> None:
        mock_cg.get_all_category_groups.return_value = [{"groupId": "g1", "name": "Essentials", "order": 0}]
        result = handler(_make_event("GET", "/api/category-groups"), None)
        assert result["statusCode"] == 200

    @patch("corderohq.personal_finance_handler._CATEGORY_GROUP_TABLE")
    def test_create_group(self, mock_cg: MagicMock) -> None:
        mock_cg.create_category_group.return_value = {"groupId": "g_new", "name": "Income", "order": 2}
        result = handler(
            _make_event("POST", "/api/category-groups", body={"name": "Income"}),
            None,
        )
        assert result["statusCode"] == 201
        mock_cg.create_category_group.assert_called_once_with("Income")

    @patch("corderohq.personal_finance_handler._CATEGORY_GROUP_TABLE")
    def test_reorder_groups(self, mock_cg: MagicMock) -> None:
        mock_cg.get_all_category_groups.return_value = []
        result = handler(
            _make_event("POST", "/api/category-groups/reorder", body={"order": ["g3", "g1", "g2"]}),
            None,
        )
        assert result["statusCode"] == 200
        mock_cg.reorder.assert_called_once_with(["g3", "g1", "g2"])

    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_GROUP_TABLE")
    def test_delete_group_blocked_by_active_categories(self, mock_cg: MagicMock, mock_ct: MagicMock) -> None:
        mock_cg.get_category_group_by_id.return_value = {"groupId": "g1", "name": "Essentials"}
        mock_ct.get_all_categories.return_value = [
            {"categoryId": "c1", "name": "Groceries", "groupId": "g1", "active": True}
        ]
        result = handler(_make_event("DELETE", "/api/category-groups/g1"), None)
        assert result["statusCode"] == 409
        body = json.loads(result["body"])
        assert body["blockingCategories"][0]["categoryId"] == "c1"
        mock_cg.delete.assert_not_called()

    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_GROUP_TABLE")
    def test_delete_group_succeeds_when_empty(self, mock_cg: MagicMock, mock_ct: MagicMock) -> None:
        mock_cg.get_category_group_by_id.return_value = {"groupId": "g1", "name": "Essentials"}
        mock_ct.get_all_categories.return_value = []
        result = handler(_make_event("DELETE", "/api/category-groups/g1"), None)
        assert result["statusCode"] == 200
        mock_cg.delete.assert_called_once_with("g1")

    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_GROUP_TABLE")
    def test_delete_group_moves_deactivated_cats_to_unassigned(self, mock_cg: MagicMock, mock_ct: MagicMock) -> None:
        # Only inactive cats live in the group → delete succeeds and they migrate
        # to the Unassigned sentinel group.
        mock_cg.get_category_group_by_id.return_value = {"groupId": "g1", "name": "Old"}
        mock_ct.get_all_categories.return_value = [
            {"categoryId": "c1", "name": "Dead1", "groupId": "g1", "active": False},
            {"categoryId": "c2", "name": "Dead2", "groupId": "g1", "active": False},
        ]
        mock_cg.get_or_create_unassigned.return_value = {"groupId": "g-sys", "name": "Unassigned", "system": True}

        result = handler(_make_event("DELETE", "/api/category-groups/g1"), None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["movedDeactivatedCategories"] == 2
        # Each inactive cat is moved into Unassigned before the group is deleted.
        assert mock_ct.move_to_group.call_count == 2
        for call in mock_ct.move_to_group.call_args_list:
            assert call.args[1] == "g-sys"
        mock_cg.delete.assert_called_once_with("g1")

    @patch("corderohq.personal_finance_handler._CATEGORY_GROUP_TABLE")
    def test_delete_system_group_forbidden(self, mock_cg: MagicMock) -> None:
        mock_cg.get_category_group_by_id.return_value = {"groupId": "g-sys", "name": "Unassigned", "system": True}
        result = handler(_make_event("DELETE", "/api/category-groups/g-sys"), None)
        assert result["statusCode"] == 403
        mock_cg.delete.assert_not_called()

    @patch("corderohq.personal_finance_handler._CATEGORY_GROUP_TABLE")
    def test_rename_system_group_forbidden(self, mock_cg: MagicMock) -> None:
        mock_cg.get_category_group_by_id.return_value = {"groupId": "g-sys", "name": "Unassigned", "system": True}
        result = handler(
            _make_event("PUT", "/api/category-groups/g-sys", body={"name": "Renamed"}),
            None,
        )
        assert result["statusCode"] == 403
        mock_cg.update.assert_not_called()

    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_GROUP_TABLE")
    def test_reorder_categories(self, mock_cg: MagicMock, mock_ct: MagicMock) -> None:
        mock_cg.get_category_group_by_id.return_value = {"groupId": "g1", "name": "Essentials"}
        mock_ct.get_all_categories.return_value = []
        result = handler(
            _make_event(
                "POST",
                "/api/categories/reorder",
                body={"groupId": "g1", "order": ["c2", "c1"]},
            ),
            None,
        )
        assert result["statusCode"] == 200
        mock_ct.reorder_in_group.assert_called_once_with("g1", ["c2", "c1"])


class TestDeactivateCategory:
    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    def test_deactivates_when_no_future_pins(self, mock_ct: MagicMock, mock_bt: MagicMock) -> None:
        mock_ct.get_category_by_id.return_value = {"categoryId": "cat1", "name": "Groceries", "active": True}
        mock_ct.deactivate.return_value = {"categoryId": "cat1", "name": "Groceries", "active": False}
        mock_bt.find_future_months_with_category.return_value = []

        result = handler(_make_event("POST", "/api/categories/cat1/deactivate", body={}), None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["affectedMonths"] == []
        mock_ct.deactivate.assert_called_once_with("cat1")

    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    def test_returns_409_when_future_pins_and_no_confirm(self, mock_ct: MagicMock, mock_bt: MagicMock) -> None:
        mock_ct.get_category_by_id.return_value = {"categoryId": "cat1", "name": "Groceries", "active": True}
        mock_bt.find_future_months_with_category.return_value = ["2026-09", "2026-12"]

        result = handler(_make_event("POST", "/api/categories/cat1/deactivate", body={}), None)

        assert result["statusCode"] == 409
        body = json.loads(result["body"])
        assert body["affectedMonths"] == ["2026-09", "2026-12"]
        mock_ct.deactivate.assert_not_called()
        mock_bt.delete_single.assert_not_called()

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    @patch("corderohq.personal_finance_handler._AUDIT_LOG_TABLE")
    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    def test_confirmed_deactivate_drops_pins_and_writes_unpin_audit(
        self,
        mock_ct: MagicMock,
        mock_bt: MagicMock,
        mock_audit: MagicMock,
        mock_tt: MagicMock,
    ) -> None:
        mock_ct.get_category_by_id.return_value = {"categoryId": "cat1", "name": "Groceries", "active": True}
        mock_ct.deactivate.return_value = {"categoryId": "cat1", "name": "Groceries", "active": False}
        mock_bt.find_future_months_with_category.return_value = ["2026-09", "2026-12"]
        mock_bt.get_targets.side_effect = [
            [{"categoryId": "cat1", "amount": 800}],
            [{"categoryId": "cat1", "amount": 1200}],
        ]

        result = handler(
            _make_event(
                "POST",
                "/api/categories/cat1/deactivate",
                body={"confirm": True, "explanation": "Cleaning up unused category"},
            ),
            None,
        )

        assert result["statusCode"] == 200
        # One UNPIN audit entry per dropped pin row.
        assert mock_audit.write_entry.call_count == 2
        # Pin rows are deleted from the Budget table.
        assert mock_bt.delete_single.call_count == 2
        # Category is then deactivated last.
        mock_ct.deactivate.assert_called_once_with("cat1")

    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    def test_returns_404_when_category_not_found(self, mock_ct: MagicMock) -> None:
        mock_ct.get_category_by_id.return_value = None
        result = handler(_make_event("POST", "/api/categories/nope/deactivate", body={}), None)
        assert result["statusCode"] == 404


_ADMIN_GROUP_FOR_HARD_DELETE = "budget-admin-test"


class TestHardDeleteCategory:
    """DELETE /api/categories/{id}, the hard delete for admins only."""

    def test_non_admin_returns_403(self) -> None:
        result = handler(
            _make_event(
                "DELETE",
                "/api/categories/cat1",
                body={"confirm": True, "confirmName": "Groceries", "explanation": "cleanup"},
            ),
            None,
        )
        assert result["statusCode"] == 403

    def test_missing_confirm_returns_400(self) -> None:
        result = handler(
            _make_event(
                "DELETE",
                "/api/categories/cat1",
                body={"confirmName": "Groceries", "explanation": "cleanup"},
                groups=[_ADMIN_GROUP_FOR_HARD_DELETE],
            ),
            None,
        )
        assert result["statusCode"] == 400

    def test_missing_explanation_returns_400(self) -> None:
        result = handler(
            _make_event(
                "DELETE",
                "/api/categories/cat1",
                body={"confirm": True, "confirmName": "Groceries"},
                groups=[_ADMIN_GROUP_FOR_HARD_DELETE],
            ),
            None,
        )
        assert result["statusCode"] == 400

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    def test_mismatched_confirm_name_returns_400(
        self,
        mock_ct: MagicMock,
        mock_bt: MagicMock,
        mock_tt: MagicMock,
    ) -> None:
        mock_ct.get_category_by_id.return_value = {"categoryId": "cat1", "name": "Groceries"}
        result = handler(
            _make_event(
                "DELETE",
                "/api/categories/cat1",
                body={"confirm": True, "confirmName": "Grocery", "explanation": "cleanup"},
                groups=[_ADMIN_GROUP_FOR_HARD_DELETE],
            ),
            None,
        )
        assert result["statusCode"] == 400
        # Make sure no destructive call was issued.
        mock_bt.delete_all_for_category.assert_not_called()
        mock_tt.delete_all_for_category.assert_not_called()
        mock_ct.delete.assert_not_called()

    @patch("corderohq.personal_finance_handler._AUDIT_LOG_TABLE")
    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    def test_happy_path_cascades_and_writes_audit_entry(
        self,
        mock_ct: MagicMock,
        mock_bt: MagicMock,
        mock_tt: MagicMock,
        mock_audit: MagicMock,
    ) -> None:
        mock_ct.get_category_by_id.return_value = {"categoryId": "cat1", "name": "Groceries"}
        mock_bt.delete_all_for_category.return_value = 14
        mock_tt.delete_all_for_category.return_value = 32

        result = handler(
            _make_event(
                "DELETE",
                "/api/categories/cat1",
                body={"confirm": True, "confirmName": "Groceries", "explanation": "Spelling error"},
                groups=[_ADMIN_GROUP_FOR_HARD_DELETE],
            ),
            None,
        )

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body == {
            "categoryId": "cat1",
            "name": "Groceries",
            "budgetRowsDeleted": 14,
            "transactionsDeleted": 32,
        }

        # Cascade order: budget → transactions → category → audit.
        mock_bt.delete_all_for_category.assert_called_once_with("cat1")
        mock_tt.delete_all_for_category.assert_called_once_with("cat1")
        mock_ct.delete.assert_called_once_with("cat1")
        kwargs = mock_audit.write_entry.call_args.kwargs
        assert kwargs["action"] == "CATEGORY_HARD_DELETE"
        assert kwargs["override"] is True
        assert kwargs["effective_year_month"] is None
        assert kwargs["changes"] == {
            "budgetRowsDeleted": 14,
            "transactionsDeleted": 32,
            "name": "Groceries",
        }


class TestGetCategoryDeletionPreview:
    def test_non_admin_returns_403(self) -> None:
        result = handler(
            _make_event(
                "GET",
                "/api/categories/cat1",
                query={"deletion_preview": "true"},
            ),
            None,
        )
        assert result["statusCode"] == 403

    def test_missing_query_param_returns_400(self) -> None:
        # Without ?deletion_preview=true, the GET route has no other purpose yet.
        result = handler(
            _make_event(
                "GET",
                "/api/categories/cat1",
                groups=[_ADMIN_GROUP_FOR_HARD_DELETE],
            ),
            None,
        )
        assert result["statusCode"] == 400

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    def test_returns_counts_and_locked_months(
        self,
        mock_ct: MagicMock,
        mock_bt: MagicMock,
        mock_tt: MagicMock,
    ) -> None:
        mock_ct.get_category_by_id.return_value = {"categoryId": "cat1", "name": "Groceries"}
        mock_bt.count_rows_for_category.return_value = 8
        mock_tt.count_rows_for_category.return_value = 17
        # Both 2026-03 and 2026-05 are LOCKED relative to _FROZEN_NOW (2026-06-15):
        # grace for 2026-05 ended on 2026-06-08, well before the frozen now. The handler
        # filters past_months by editability() == "LOCKED" — both should survive.
        mock_bt.find_past_months_with_category.return_value = ["2026-03", "2026-05"]

        result = handler(
            _make_event(
                "GET",
                "/api/categories/cat1",
                query={"deletion_preview": "true"},
                groups=[_ADMIN_GROUP_FOR_HARD_DELETE],
            ),
            None,
        )

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["deletionPreview"]["budgetRows"] == 8
        assert body["deletionPreview"]["transactions"] == 17
        assert body["deletionPreview"]["lockedMonthsAffected"] == ["2026-03", "2026-05"]


class TestGetBudget:
    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    def test_dense_month_returns_stored_rows_directly(self, mock_bt: MagicMock, mock_ct: MagicMock) -> None:
        # _EDITABLE_MONTH == current ym → dense, no walk-back.
        mock_bt.scan_all.return_value = [{"yearMonth": _EDITABLE_MONTH, "categoryId": "cat1", "amount": 500}]
        mock_bt.get_targets.return_value = [{"yearMonth": _EDITABLE_MONTH, "categoryId": "cat1", "amount": 500}]
        mock_ct.get_all_categories.return_value = [{"categoryId": "cat1", "name": "Groceries", "active": True}]

        result = handler(_make_event("GET", f"/api/budget/{_EDITABLE_MONTH}"), None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body == [{"yearMonth": _EDITABLE_MONTH, "categoryId": "cat1", "amount": 500}]

    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    def test_future_month_returns_walkback_resolved_with_pinned_flag(
        self, mock_bt: MagicMock, mock_ct: MagicMock
    ) -> None:
        # Current = 2026-06, asking for 2026-09 (future). No pin at 09 → walk back to 06.
        mock_bt.scan_all.return_value = [{"yearMonth": _EDITABLE_MONTH, "categoryId": "cat1", "amount": 500}]
        mock_ct.get_all_categories.return_value = [{"categoryId": "cat1", "name": "Groceries", "active": True}]

        result = handler(_make_event("GET", "/api/budget/2026-09"), None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body == [{"yearMonth": "2026-09", "categoryId": "cat1", "amount": 500, "pinned": False}]

    def test_returns_400_for_invalid_month(self) -> None:
        result = handler(_make_event("GET", "/api/budget/invalid"), None)
        assert result["statusCode"] == 400


_FUTURE_MONTH = "2026-09"


class TestPostBudgetReplace:
    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    @patch("corderohq.personal_finance_handler._AUDIT_LOG_TABLE")
    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    def test_sets_targets_and_writes_audit(self, mock_bt: MagicMock, mock_audit: MagicMock, mock_tt: MagicMock) -> None:
        mock_bt.get_targets.return_value = []
        mock_bt.put_targets.return_value = [{"yearMonth": _EDITABLE_MONTH, "categoryId": "cat1", "amount": 500}]
        mock_tt.list_for_month.return_value = []
        mock_audit.write_entry.return_value = {}

        result = handler(
            _make_event(
                "POST",
                f"/api/budget/{_EDITABLE_MONTH}/replace",
                body={
                    "targets": [{"categoryId": "cat1", "amount": 500}],
                    "explanation": "Initial budget setup",
                },
            ),
            None,
        )
        assert result["statusCode"] == 200
        mock_audit.write_entry.assert_called_once()
        # New row → CREATE entry.
        assert mock_audit.write_entry.call_args.kwargs["action"] == "CREATE"

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    @patch("corderohq.personal_finance_handler._AUDIT_LOG_TABLE")
    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    def test_emits_update_audit_when_amount_changes(
        self, mock_bt: MagicMock, mock_audit: MagicMock, mock_tt: MagicMock
    ) -> None:
        mock_bt.get_targets.return_value = [{"yearMonth": _EDITABLE_MONTH, "categoryId": "cat1", "amount": 500}]
        mock_bt.put_targets.return_value = [{"yearMonth": _EDITABLE_MONTH, "categoryId": "cat1", "amount": 750}]
        mock_tt.list_for_month.return_value = []
        mock_audit.write_entry.return_value = {}

        handler(
            _make_event(
                "POST",
                f"/api/budget/{_EDITABLE_MONTH}/replace",
                body={
                    "targets": [{"categoryId": "cat1", "amount": 750}],
                    "explanation": "Bumping grocery target",
                },
            ),
            None,
        )
        kwargs = mock_audit.write_entry.call_args.kwargs
        assert kwargs["action"] == "UPDATE"
        assert kwargs["changes"] == {"amount": {"before": 500, "after": 750}}

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    @patch("corderohq.personal_finance_handler._AUDIT_LOG_TABLE")
    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    def test_skips_audit_for_unchanged_amount(
        self, mock_bt: MagicMock, mock_audit: MagicMock, mock_tt: MagicMock
    ) -> None:
        mock_bt.get_targets.return_value = [{"yearMonth": _EDITABLE_MONTH, "categoryId": "cat1", "amount": 500}]
        mock_bt.put_targets.return_value = [{"yearMonth": _EDITABLE_MONTH, "categoryId": "cat1", "amount": 500}]
        mock_tt.list_for_month.return_value = []

        handler(
            _make_event(
                "POST",
                f"/api/budget/{_EDITABLE_MONTH}/replace",
                body={
                    "targets": [{"categoryId": "cat1", "amount": 500}],
                    "explanation": "No-op resubmit",
                },
            ),
            None,
        )
        mock_audit.write_entry.assert_not_called()

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    def test_returns_409_when_body_missing_existing_category(self, mock_bt: MagicMock, mock_tt: MagicMock) -> None:
        # cat2 has an existing row but isn't in the body → body-completeness violation.
        mock_bt.get_targets.return_value = [
            {"yearMonth": _EDITABLE_MONTH, "categoryId": "cat1", "amount": 500},
            {"yearMonth": _EDITABLE_MONTH, "categoryId": "cat2", "amount": 300},
        ]
        mock_tt.list_for_month.return_value = []

        result = handler(
            _make_event(
                "POST",
                f"/api/budget/{_EDITABLE_MONTH}/replace",
                body={
                    "targets": [{"categoryId": "cat1", "amount": 600}],
                    "explanation": "Forgot cat2",
                },
            ),
            None,
        )
        assert result["statusCode"] == 409
        body = json.loads(result["body"])
        assert body["missingCategoryIds"] == ["cat2"]
        mock_bt.put_targets.assert_not_called()

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    def test_returns_409_when_body_missing_category_with_nonzero_transaction(
        self, mock_bt: MagicMock, mock_tt: MagicMock
    ) -> None:
        # cat2 has a transaction but no existing row → still required in body.
        mock_bt.get_targets.return_value = []
        mock_tt.list_for_month.return_value = [
            {"yearMonth": _EDITABLE_MONTH, "categoryId": "cat2", "amount": 5000000},
        ]

        result = handler(
            _make_event(
                "POST",
                f"/api/budget/{_EDITABLE_MONTH}/replace",
                body={
                    "targets": [{"categoryId": "cat1", "amount": 500}],
                    "explanation": "Missing cat2 which has a txn",
                },
            ),
            None,
        )
        assert result["statusCode"] == 409
        body = json.loads(result["body"])
        assert body["missingCategoryIds"] == ["cat2"]

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    @patch("corderohq.personal_finance_handler._AUDIT_LOG_TABLE")
    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    def test_zero_amount_transaction_not_required_in_body(
        self, mock_bt: MagicMock, mock_audit: MagicMock, mock_tt: MagicMock
    ) -> None:
        mock_bt.get_targets.return_value = []
        mock_bt.put_targets.return_value = [{"yearMonth": _EDITABLE_MONTH, "categoryId": "cat1", "amount": 500}]
        mock_tt.list_for_month.return_value = [
            {"yearMonth": _EDITABLE_MONTH, "categoryId": "cat2", "amount": 0},
        ]
        mock_audit.write_entry.return_value = {}

        result = handler(
            _make_event(
                "POST",
                f"/api/budget/{_EDITABLE_MONTH}/replace",
                body={
                    "targets": [{"categoryId": "cat1", "amount": 500}],
                    "explanation": "cat2 only has $0 txn so it's not required",
                },
            ),
            None,
        )
        assert result["statusCode"] == 200

    def test_returns_409_for_future_month(self) -> None:
        # /replace is for dense months only; future months go to /pin.
        result = handler(
            _make_event(
                "POST",
                f"/api/budget/{_FUTURE_MONTH}/replace",
                body={
                    "targets": [{"categoryId": "cat1", "amount": 500}],
                    "explanation": "Trying to use replace for a future month",
                },
            ),
            None,
        )
        assert result["statusCode"] == 409
        body = json.loads(result["body"])
        assert body["yearMonth"] == _FUTURE_MONTH
        assert "use /pin" in body["error"]

    def test_returns_400_when_explanation_missing(self) -> None:
        result = handler(
            _make_event(
                "POST",
                f"/api/budget/{_EDITABLE_MONTH}/replace",
                body={
                    "targets": [{"categoryId": "cat1", "amount": 500}],
                },
            ),
            None,
        )
        assert result["statusCode"] == 400
        assert "explanation" in result["body"]

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    @patch("corderohq.personal_finance_handler._AUDIT_LOG_TABLE")
    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    def test_audit_entry_snapshots_jwt_user_identity(
        self, mock_bt: MagicMock, mock_audit: MagicMock, mock_tt: MagicMock
    ) -> None:
        # The audit row's `user` is a {sub, email, username} snapshot drawn from
        # the JWT claims at request time — not just an email string. See the
        # "Audit user identity" section in docs/personal-finance-architecture.md.
        mock_bt.get_targets.return_value = []
        mock_bt.put_targets.return_value = [{"yearMonth": _EDITABLE_MONTH, "categoryId": "cat1", "amount": 500}]
        mock_tt.list_for_month.return_value = []
        mock_audit.write_entry.return_value = {}

        event = _make_event(
            "POST",
            f"/api/budget/{_EDITABLE_MONTH}/replace",
            body={
                "targets": [{"categoryId": "cat1", "amount": 500}],
                "explanation": "Initial budget setup",
            },
        )
        event["requestContext"]["authorizer"]["jwt"]["claims"].update(
            {"sub": "user-sub-abc", "cognito:username": "alice"}
        )

        handler(event, None)

        kwargs = mock_audit.write_entry.call_args.kwargs
        assert kwargs["user"] == {
            "sub": "user-sub-abc",
            "email": "test@example.com",
            "username": "alice",
        }


class TestGetTransactions:
    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    def test_returns_transactions_for_month(self, mock_tt: MagicMock) -> None:
        mock_tt.list_for_month.return_value = [{"yearMonth": _EDITABLE_MONTH, "description": "Coffee"}]
        result = handler(_make_event("GET", "/api/transactions", query={"month": _EDITABLE_MONTH}), None)
        assert result["statusCode"] == 200

    def test_returns_400_when_month_missing(self) -> None:
        result = handler(_make_event("GET", "/api/transactions"), None)
        assert result["statusCode"] == 400


class TestPostTransactionsCommit:
    """Atomic CSV commit: all-or-nothing pre-flight validation."""

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    def test_happy_path_batch_creates(self, mock_ct: MagicMock, mock_tt: MagicMock) -> None:
        mock_ct.get_all_categories.return_value = [{"categoryId": "cat1", "name": "Groceries", "active": True}]
        mock_tt.batch_create.return_value = [
            {
                "yearMonth": _EDITABLE_MONTH,
                "sortId": "2026-06-15#a",
                "amount": 50,
                "transactionDate": "2026-06-15",
                "description": "x",
                "categoryId": "cat1",
            },
        ]

        result = handler(
            _make_event(
                "POST",
                "/api/transactions/commit",
                body={
                    "rows": [
                        {
                            "transactionDate": f"{_EDITABLE_MONTH}-15",
                            "description": "Coffee",
                            "amount": 5_000_000,
                            "categoryId": "cat1",
                        },
                    ],
                },
            ),
            None,
        )

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["count"] == 1
        mock_tt.batch_create.assert_called_once()

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    def test_aborts_when_any_row_invalid_and_returns_full_validation_list(
        self, mock_ct: MagicMock, mock_tt: MagicMock
    ) -> None:
        # cat1 exists; row 1 has unknown categoryId; row 2 targets a locked month.
        mock_ct.get_all_categories.return_value = [{"categoryId": "cat1", "name": "Groceries", "active": True}]

        result = handler(
            _make_event(
                "POST",
                "/api/transactions/commit",
                body={
                    "rows": [
                        {
                            "transactionDate": f"{_EDITABLE_MONTH}-10",
                            "description": "good row",
                            "amount": 1_000_000,
                            "categoryId": "cat1",
                        },
                        {
                            "transactionDate": f"{_EDITABLE_MONTH}-12",
                            "description": "bad category",
                            "amount": 2_000_000,
                            "categoryId": "unknown",
                        },
                        {
                            "transactionDate": f"{_LOCKED_MONTH}-05",
                            "description": "locked month",
                            "amount": 3_000_000,
                            "categoryId": "cat1",
                        },
                    ],
                },
            ),
            None,
        )

        assert result["statusCode"] == 409
        body = json.loads(result["body"])
        # Every row's status, including the good one, is in the list — no one-at-a-time.
        assert len(body["validations"]) == 3
        issues_by_index = {v["index"]: v["issues"] for v in body["validations"]}
        assert issues_by_index[0] == []
        assert "missing_category" in issues_by_index[1]
        assert "locked_month" in issues_by_index[2]
        mock_tt.batch_create.assert_not_called()

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    def test_override_with_admin_bypasses_locked_month_check(self, mock_ct: MagicMock, mock_tt: MagicMock) -> None:
        mock_ct.get_all_categories.return_value = [{"categoryId": "cat1", "name": "Groceries", "active": True}]
        mock_tt.batch_create.return_value = [
            {
                "yearMonth": _LOCKED_MONTH,
                "sortId": "x",
                "amount": 1,
                "transactionDate": "x",
                "description": "x",
                "categoryId": "cat1",
            },
        ]

        result = handler(
            _make_event(
                "POST",
                "/api/transactions/commit",
                body={
                    "rows": [
                        {
                            "transactionDate": f"{_LOCKED_MONTH}-05",
                            "description": "Locked-month commit",
                            "amount": 1_000_000,
                            "categoryId": "cat1",
                        },
                    ],
                    "override": True,
                },
                groups=[_ADMIN_GROUP_FOR_HARD_DELETE],
            ),
            None,
        )

        assert result["statusCode"] == 200
        mock_tt.batch_create.assert_called_once()

    def test_missing_rows_returns_400(self) -> None:
        result = handler(_make_event("POST", "/api/transactions/commit", body={}), None)
        assert result["statusCode"] == 400


class TestPostTransaction:
    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    def test_creates_transaction(self, mock_tt: MagicMock) -> None:
        mock_tt.create.return_value = {
            "yearMonth": _EDITABLE_MONTH,
            "sortId": "2026-06-15#abc",
            "description": "Coffee",
        }
        result = handler(
            _make_event(
                "POST",
                "/api/transactions",
                body={
                    "yearMonth": _EDITABLE_MONTH,
                    "transactionDate": "2026-06-15",
                    "description": "Coffee",
                    "amount": 5000000,
                    "categoryId": "cat1",
                },
            ),
            None,
        )
        assert result["statusCode"] == 201

    def test_returns_400_when_field_missing(self) -> None:
        result = handler(
            _make_event("POST", "/api/transactions", body={"yearMonth": _EDITABLE_MONTH}),
            None,
        )
        assert result["statusCode"] == 400


class TestUpdateTransaction:
    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    def test_updates_transaction(self, mock_tt: MagicMock) -> None:
        mock_tt.update.return_value = {
            "yearMonth": _EDITABLE_MONTH,
            "sortId": "2026-06-15#abc",
            "description": "Updated",
        }
        result = handler(
            _make_event(
                "POST",
                "/api/transactions/update",
                body={
                    "yearMonth": _EDITABLE_MONTH,
                    "sortId": "2026-06-15#abc",
                    "description": "Updated",
                },
            ),
            None,
        )
        assert result["statusCode"] == 200

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    def test_returns_404_when_not_found(self, mock_tt: MagicMock) -> None:
        mock_tt.update.return_value = None
        result = handler(
            _make_event(
                "POST",
                "/api/transactions/update",
                body={
                    "yearMonth": _EDITABLE_MONTH,
                    "sortId": "nonexistent",
                    "description": "X",
                },
            ),
            None,
        )
        assert result["statusCode"] == 404

    def test_returns_400_when_keys_missing(self) -> None:
        result = handler(
            _make_event("POST", "/api/transactions/update", body={"description": "X"}),
            None,
        )
        assert result["statusCode"] == 400


class TestDeleteTransactions:
    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    def test_batch_deletes_transactions(self, mock_tt: MagicMock) -> None:
        mock_tt.batch_delete.return_value = 2
        result = handler(
            _make_event(
                "POST",
                "/api/transactions/delete",
                body={
                    "items": [
                        {"yearMonth": _EDITABLE_MONTH, "sortId": "2026-06-15#abc"},
                        {"yearMonth": _EDITABLE_MONTH, "sortId": "2026-06-16#def"},
                    ],
                },
            ),
            None,
        )
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["deleted"] == 2

    def test_returns_400_when_items_missing(self) -> None:
        result = handler(_make_event("POST", "/api/transactions/delete", body={}), None)
        assert result["statusCode"] == 400

    def test_returns_400_when_items_empty(self) -> None:
        result = handler(_make_event("POST", "/api/transactions/delete", body={"items": []}), None)
        assert result["statusCode"] == 400


class TestGetSummary:
    @patch("corderohq.personal_finance_handler.compute_summary")
    def test_returns_summary(self, mock_summary: MagicMock) -> None:
        mock_summary.return_value = {
            "yearMonth": _EDITABLE_MONTH,
            "categories": [],
            "totals": {"budgeted": 0, "actual": 0, "delta": 0},
        }
        result = handler(_make_event("GET", "/api/summary", query={"month": _EDITABLE_MONTH}), None)
        assert result["statusCode"] == 200

    def test_returns_400_when_month_missing(self) -> None:
        result = handler(_make_event("GET", "/api/summary"), None)
        assert result["statusCode"] == 400


class TestGetAuditLog:
    @patch("corderohq.personal_finance_handler._AUDIT_LOG_TABLE")
    def test_returns_audit_entries(self, mock_audit: MagicMock) -> None:
        mock_audit.read_recent_entries.return_value = [{"sortId": "01AAA", "action": "CREATE"}]
        result = handler(_make_event("GET", "/api/audit-log"), None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body) == 1

    @patch("corderohq.personal_finance_handler._AUDIT_LOG_TABLE")
    def test_respects_limit_param(self, mock_audit: MagicMock) -> None:
        mock_audit.read_recent_entries.return_value = []
        handler(_make_event("GET", "/api/audit-log", query={"limit": "10"}), None)
        mock_audit.read_recent_entries.assert_called_once_with(10)


class TestPostBudgetPin:
    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    @patch("corderohq.personal_finance_handler._AUDIT_LOG_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    def test_pins_new_future_row(
        self,
        mock_bt: MagicMock,
        mock_ct: MagicMock,
        mock_audit: MagicMock,
        mock_tt: MagicMock,
    ) -> None:
        # No existing pin; current=2026-06, pin 2026-09 cat1=500.
        mock_bt.scan_all.return_value = []
        mock_ct.get_all_categories.return_value = [{"categoryId": "cat1", "name": "Groceries", "active": True}]

        result = handler(
            _make_event(
                "POST",
                f"/api/budget/{_FUTURE_MONTH}/pin",
                body={
                    "targets": [{"categoryId": "cat1", "amount": 500}],
                    "explanation": "Holiday spending bump",
                },
            ),
            None,
        )
        assert result["statusCode"] == 200
        mock_bt.put_single.assert_called_once_with(_FUTURE_MONTH, "cat1", 500)

        audit_kwargs = mock_audit.write_entry.call_args.kwargs
        assert audit_kwargs["action"] == "PIN"
        assert audit_kwargs["changes"] == {"amount": {"before": None, "after": 500}}

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    @patch("corderohq.personal_finance_handler._AUDIT_LOG_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    def test_unpin_with_null_amount_emits_unpin_entry(
        self,
        mock_bt: MagicMock,
        mock_ct: MagicMock,
        mock_audit: MagicMock,
        mock_tt: MagicMock,
    ) -> None:
        mock_bt.scan_all.return_value = [
            {"yearMonth": _FUTURE_MONTH, "categoryId": "cat1", "amount": 800},
        ]
        mock_ct.get_all_categories.return_value = [{"categoryId": "cat1", "name": "Groceries", "active": True}]

        handler(
            _make_event(
                "POST",
                f"/api/budget/{_FUTURE_MONTH}/pin",
                body={
                    "targets": [{"categoryId": "cat1", "amount": None}],
                    "explanation": "Remove holiday pin",
                },
            ),
            None,
        )

        mock_bt.delete_single.assert_called_once_with(_FUTURE_MONTH, "cat1")
        kwargs = mock_audit.write_entry.call_args.kwargs
        assert kwargs["action"] == "UNPIN"
        assert kwargs["changes"] == {"amount": {"before": 800, "after": None}}

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    @patch("corderohq.personal_finance_handler._AUDIT_LOG_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    def test_same_value_pin_is_noop(
        self,
        mock_bt: MagicMock,
        mock_ct: MagicMock,
        mock_audit: MagicMock,
        mock_tt: MagicMock,
    ) -> None:
        # Re-submitting the same pin value should not write or audit.
        mock_bt.scan_all.return_value = [
            {"yearMonth": _FUTURE_MONTH, "categoryId": "cat1", "amount": 500},
        ]
        mock_ct.get_all_categories.return_value = [{"categoryId": "cat1", "name": "Groceries", "active": True}]

        handler(
            _make_event(
                "POST",
                f"/api/budget/{_FUTURE_MONTH}/pin",
                body={
                    "targets": [{"categoryId": "cat1", "amount": 500}],
                    "explanation": "Resubmit identical pin",
                },
            ),
            None,
        )
        mock_bt.put_single.assert_not_called()
        mock_audit.write_entry.assert_not_called()

    def test_rejects_dense_month(self) -> None:
        # Current month → /replace territory.
        result = handler(
            _make_event(
                "POST",
                f"/api/budget/{_EDITABLE_MONTH}/pin",
                body={
                    "targets": [{"categoryId": "cat1", "amount": 500}],
                    "explanation": "Wrong endpoint for current month",
                },
            ),
            None,
        )
        assert result["statusCode"] == 409
        body = json.loads(result["body"])
        assert "use /replace" in body["error"]

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    @patch("corderohq.personal_finance_handler._AUDIT_LOG_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    def test_pin_matches_carried_value_warning(
        self,
        mock_bt: MagicMock,
        mock_ct: MagicMock,
        mock_audit: MagicMock,
        mock_tt: MagicMock,
    ) -> None:
        # Current=06 with cat1=500. Pinning 2026-09=500 → walk-back without the new pin yields 500.
        mock_bt.scan_all.return_value = [
            {"yearMonth": _EDITABLE_MONTH, "categoryId": "cat1", "amount": 500},
        ]
        mock_ct.get_all_categories.return_value = [{"categoryId": "cat1", "name": "Groceries", "active": True}]

        result = handler(
            _make_event(
                "POST",
                f"/api/budget/{_FUTURE_MONTH}/pin",
                body={
                    "targets": [{"categoryId": "cat1", "amount": 500}],
                    "explanation": "Pin to the value that would have carried forward anyway",
                },
            ),
            None,
        )
        body = json.loads(result["body"])
        assert "cat1" in body["warnings"]["pinMatchesCarriedValue"]

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    @patch("corderohq.personal_finance_handler._AUDIT_LOG_TABLE")
    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    def test_downstream_pins_warning(
        self,
        mock_bt: MagicMock,
        mock_ct: MagicMock,
        mock_audit: MagicMock,
        mock_tt: MagicMock,
    ) -> None:
        # Existing pin at 2026-12 (downstream of 2026-09 target). Should appear in warnings.
        # scan_all called twice: before writes (no 2026-12 cat1) and after (still has the
        # untouched 2026-12 cat1 pin).
        downstream_state = [
            {"yearMonth": _EDITABLE_MONTH, "categoryId": "cat1", "amount": 500},
            {"yearMonth": "2026-12", "categoryId": "cat1", "amount": 999},
        ]
        mock_bt.scan_all.return_value = downstream_state
        mock_ct.get_all_categories.return_value = [{"categoryId": "cat1", "name": "Groceries", "active": True}]

        result = handler(
            _make_event(
                "POST",
                f"/api/budget/{_FUTURE_MONTH}/pin",
                body={
                    "targets": [{"categoryId": "cat1", "amount": 700}],
                    "explanation": "Pin upstream of an existing future pin",
                },
            ),
            None,
        )
        body = json.loads(result["body"])
        assert body["warnings"]["downstreamPins"] == [{"categoryId": "cat1", "yearMonth": "2026-12"}]


_ADMIN_GROUP = "budget-admin-test"


class TestEditabilityEnforcement:
    """The lock/grace state machine wired into every mutation endpoint."""

    # --- _post_budget_replace ---

    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    def test_replace_locked_month_returns_409(self, mock_bt: MagicMock) -> None:
        result = handler(
            _make_event(
                "POST",
                f"/api/budget/{_LOCKED_MONTH}/replace",
                body={
                    "targets": [{"categoryId": "cat1", "amount": 500}],
                    "explanation": "Adjusting past month",
                },
            ),
            None,
        )
        assert result["statusCode"] == 409
        body = json.loads(result["body"])
        assert body["yearMonth"] == _LOCKED_MONTH
        assert body["state"] == "LOCKED"
        mock_bt.put_targets.assert_not_called()

    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    def test_replace_override_without_admin_returns_403(self, mock_bt: MagicMock) -> None:
        result = handler(
            _make_event(
                "POST",
                f"/api/budget/{_LOCKED_MONTH}/replace",
                body={
                    "targets": [{"categoryId": "cat1", "amount": 500}],
                    "explanation": "Adjusting past month",
                    "override": True,
                },
            ),
            None,
        )
        assert result["statusCode"] == 403
        mock_bt.put_targets.assert_not_called()

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    @patch("corderohq.personal_finance_handler._AUDIT_LOG_TABLE")
    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    def test_replace_override_with_admin_succeeds_and_audit_flagged(
        self, mock_bt: MagicMock, mock_audit: MagicMock, mock_tt: MagicMock
    ) -> None:
        mock_bt.get_targets.return_value = []
        mock_bt.put_targets.return_value = [{"yearMonth": _LOCKED_MONTH, "categoryId": "cat1", "amount": 500}]
        mock_tt.list_for_month.return_value = []
        mock_audit.write_entry.return_value = {}

        result = handler(
            _make_event(
                "POST",
                f"/api/budget/{_LOCKED_MONTH}/replace",
                body={
                    "targets": [{"categoryId": "cat1", "amount": 500}],
                    "explanation": "Backdated correction for an old month",
                    "override": True,
                },
                groups=[_ADMIN_GROUP],
            ),
            None,
        )
        assert result["statusCode"] == 200
        mock_audit.write_entry.assert_called_once()
        kwargs = mock_audit.write_entry.call_args.kwargs
        assert kwargs["override"] is True

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    @patch("corderohq.personal_finance_handler._AUDIT_LOG_TABLE")
    @patch("corderohq.personal_finance_handler._BUDGET_TABLE")
    def test_replace_editable_month_audit_override_is_false(
        self, mock_bt: MagicMock, mock_audit: MagicMock, mock_tt: MagicMock
    ) -> None:
        mock_bt.get_targets.return_value = []
        mock_bt.put_targets.return_value = [{"yearMonth": _EDITABLE_MONTH, "categoryId": "cat1", "amount": 500}]
        mock_tt.list_for_month.return_value = []
        mock_audit.write_entry.return_value = {}

        handler(
            _make_event(
                "POST",
                f"/api/budget/{_EDITABLE_MONTH}/replace",
                body={
                    "targets": [{"categoryId": "cat1", "amount": 500}],
                    "explanation": "Setting initial budget",
                },
            ),
            None,
        )
        kwargs = mock_audit.write_entry.call_args.kwargs
        assert kwargs["override"] is False

    # --- _post_transaction ---

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    def test_post_transaction_locked_month_returns_409(self, mock_tt: MagicMock) -> None:
        result = handler(
            _make_event(
                "POST",
                "/api/transactions",
                body={
                    "yearMonth": _LOCKED_MONTH,
                    "transactionDate": f"{_LOCKED_MONTH}-15",
                    "description": "Coffee",
                    "amount": 5000000,
                    "categoryId": "cat1",
                },
            ),
            None,
        )
        assert result["statusCode"] == 409
        mock_tt.create.assert_not_called()

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    def test_post_transaction_override_with_admin_succeeds(self, mock_tt: MagicMock) -> None:
        mock_tt.create.return_value = {"yearMonth": _LOCKED_MONTH, "sortId": "2026-03-15#abc"}
        result = handler(
            _make_event(
                "POST",
                "/api/transactions",
                body={
                    "yearMonth": _LOCKED_MONTH,
                    "transactionDate": f"{_LOCKED_MONTH}-15",
                    "description": "Backdated coffee",
                    "amount": 5000000,
                    "categoryId": "cat1",
                    "override": True,
                },
                groups=[_ADMIN_GROUP],
            ),
            None,
        )
        assert result["statusCode"] == 201

    # --- _put_transaction (update) ---

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    def test_update_transaction_locked_month_returns_409(self, mock_tt: MagicMock) -> None:
        result = handler(
            _make_event(
                "POST",
                "/api/transactions/update",
                body={
                    "yearMonth": _LOCKED_MONTH,
                    "sortId": "2026-03-15#abc",
                    "description": "Updated",
                },
            ),
            None,
        )
        assert result["statusCode"] == 409
        mock_tt.update.assert_not_called()

    # --- _delete_transaction (batch) ---

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    def test_delete_transactions_locked_month_returns_409_with_all_offending(self, mock_tt: MagicMock) -> None:
        result = handler(
            _make_event(
                "POST",
                "/api/transactions/delete",
                body={
                    "items": [
                        {"yearMonth": _LOCKED_MONTH, "sortId": "2026-03-15#abc"},
                        {"yearMonth": _EDITABLE_MONTH, "sortId": "2026-06-15#def"},
                        {"yearMonth": _LOCKED_MONTH, "sortId": "2026-03-20#ghi"},
                    ],
                },
            ),
            None,
        )
        assert result["statusCode"] == 409
        body = json.loads(result["body"])
        # Both locked items should appear in the offending list, with their indices.
        offending = body["items"]
        assert len(offending) == 2
        assert {item["index"] for item in offending} == {0, 2}
        mock_tt.batch_delete.assert_not_called()

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    def test_delete_transactions_override_with_admin_succeeds(self, mock_tt: MagicMock) -> None:
        mock_tt.batch_delete.return_value = 1
        result = handler(
            _make_event(
                "POST",
                "/api/transactions/delete",
                body={
                    "items": [{"yearMonth": _LOCKED_MONTH, "sortId": "2026-03-15#abc"}],
                    "override": True,
                },
                groups=[_ADMIN_GROUP],
            ),
            None,
        )
        assert result["statusCode"] == 200

    @patch("corderohq.personal_finance_handler._TRANSACTIONS_TABLE")
    def test_delete_transactions_override_without_admin_returns_403(self, mock_tt: MagicMock) -> None:
        result = handler(
            _make_event(
                "POST",
                "/api/transactions/delete",
                body={
                    "items": [{"yearMonth": _LOCKED_MONTH, "sortId": "2026-03-15#abc"}],
                    "override": True,
                },
            ),
            None,
        )
        assert result["statusCode"] == 403
        mock_tt.batch_delete.assert_not_called()


class TestNotFound:
    def test_unknown_path_returns_404(self) -> None:
        result = handler(_make_event("GET", "/api/unknown"), None)
        assert result["statusCode"] == 404


class TestErrorHandling:
    @patch("corderohq.personal_finance_handler._CATEGORY_TABLE")
    def test_unhandled_error_returns_500(self, mock_ct: MagicMock) -> None:
        mock_ct.get_active_categories.side_effect = RuntimeError("boom")
        result = handler(_make_event("GET", "/api/categories"), None)
        assert result["statusCode"] == 500
        assert result["body"] == "Internal server error"


# --- Household profile (net worth) ---


class TestGetProfile:
    @patch("corderohq.personal_finance_handler._PROFILE_TABLE")
    def test_returns_people(self, mock_pt: MagicMock) -> None:
        mock_pt.list_people.return_value = [{"personId": "p1", "name": "Jordan", "birthYearMonth": "1985-07"}]
        result = handler(_make_event("GET", "/api/profile"), None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body[0]["name"] == "Jordan"


class TestPostPerson:
    @patch("corderohq.personal_finance_handler._PROFILE_TABLE")
    def test_creates_person(self, mock_pt: MagicMock) -> None:
        mock_pt.create_person.return_value = {"personId": "p1", "name": "Jordan", "birthYearMonth": "1985-07"}
        result = handler(
            _make_event("POST", "/api/profile/people", body={"name": "Jordan", "birthYearMonth": "1985-07"}),
            None,
        )
        assert result["statusCode"] == 201
        mock_pt.create_person.assert_called_once_with("Jordan", "1985-07")

    def test_requires_name(self) -> None:
        result = handler(_make_event("POST", "/api/profile/people", body={"birthYearMonth": "1985-07"}), None)
        assert result["statusCode"] == 400

    def test_requires_birth_year_month(self) -> None:
        result = handler(_make_event("POST", "/api/profile/people", body={"name": "Jordan"}), None)
        assert result["statusCode"] == 400

    @patch("corderohq.personal_finance_handler._PROFILE_TABLE")
    def test_invalid_birth_month_returns_400(self, mock_pt: MagicMock) -> None:
        mock_pt.create_person.side_effect = ValueError("birthYearMonth must be a YYYY-MM string")
        result = handler(
            _make_event("POST", "/api/profile/people", body={"name": "Jordan", "birthYearMonth": "bad"}),
            None,
        )
        assert result["statusCode"] == 400


class TestPutPerson:
    @patch("corderohq.personal_finance_handler._PROFILE_TABLE")
    def test_updates_person(self, mock_pt: MagicMock) -> None:
        mock_pt.update_person.return_value = {"personId": "p1", "name": "New", "birthYearMonth": "1985-07"}
        result = handler(_make_event("PUT", "/api/profile/people/p1", body={"name": "New"}), None)
        assert result["statusCode"] == 200
        mock_pt.update_person.assert_called_once_with("p1", name="New", birth_year_month=None)

    def test_requires_a_field(self) -> None:
        result = handler(_make_event("PUT", "/api/profile/people/p1", body={}), None)
        assert result["statusCode"] == 400

    @patch("corderohq.personal_finance_handler._PROFILE_TABLE")
    def test_missing_person_returns_404(self, mock_pt: MagicMock) -> None:
        mock_pt.update_person.return_value = None
        result = handler(_make_event("PUT", "/api/profile/people/nope", body={"name": "New"}), None)
        assert result["statusCode"] == 404


class TestDeletePerson:
    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    @patch("corderohq.personal_finance_handler._PROFILE_TABLE")
    def test_deletes_when_unreferenced(self, mock_pt: MagicMock, mock_at: MagicMock) -> None:
        mock_pt.get_person.return_value = {"personId": "p1", "name": "Jordan"}
        mock_at.accounts_with_owner.return_value = []
        result = handler(_make_event("POST", "/api/profile/people/p1/delete"), None)
        assert result["statusCode"] == 200
        mock_pt.delete_person.assert_called_once_with("p1")

    @patch("corderohq.personal_finance_handler._PROFILE_TABLE")
    def test_missing_person_returns_404(self, mock_pt: MagicMock) -> None:
        mock_pt.get_person.return_value = None
        result = handler(_make_event("POST", "/api/profile/people/nope/delete"), None)
        assert result["statusCode"] == 404

    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    @patch("corderohq.personal_finance_handler._PROFILE_TABLE")
    def test_blocked_when_owner_of_accounts(self, mock_pt: MagicMock, mock_at: MagicMock) -> None:
        mock_pt.get_person.return_value = {"personId": "p1", "name": "Jordan"}
        mock_at.accounts_with_owner.return_value = [{"accountId": "a1", "name": "His 401k"}]
        result = handler(_make_event("POST", "/api/profile/people/p1/delete"), None)
        assert result["statusCode"] == 409
        body = json.loads(result["body"])
        assert body["blockingAccounts"][0]["accountId"] == "a1"
        mock_pt.delete_person.assert_not_called()


# --- Accounts (net worth) ---


class TestGetAccounts:
    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    def test_returns_active_by_default(self, mock_at: MagicMock) -> None:
        mock_at.get_active_accounts.return_value = [{"accountId": "a1", "name": "Chase Checking"}]
        result = handler(_make_event("GET", "/api/accounts"), None)
        assert result["statusCode"] == 200
        mock_at.get_active_accounts.assert_called_once()
        mock_at.get_all_accounts.assert_not_called()

    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    def test_include_inactive_lists_all(self, mock_at: MagicMock) -> None:
        mock_at.get_all_accounts.return_value = []
        result = handler(_make_event("GET", "/api/accounts", query={"include_inactive": "true"}), None)
        assert result["statusCode"] == 200
        mock_at.get_all_accounts.assert_called_once()


class TestPostAccount:
    @patch("corderohq.personal_finance_handler._PROFILE_TABLE")
    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    def test_creates_asset_account(self, mock_at: MagicMock, mock_pt: MagicMock) -> None:
        mock_pt.get_person.return_value = {"personId": "p1", "name": "Jordan"}
        mock_at.create_account.return_value = {"accountId": "a1", "name": "Chase Checking"}
        result = handler(
            _make_event(
                "POST",
                "/api/accounts",
                body={
                    "name": "Chase Checking",
                    "accountType": "checking",
                    "assetClasses": ["cash"],
                    "owners": ["p1"],
                },
            ),
            None,
        )
        assert result["statusCode"] == 201
        mock_at.create_account.assert_called_once_with(
            CreateAccountArgs(
                name="Chase Checking",
                owners=["p1"],
                excluded_from_net_worth=False,
                notes=None,
                account=FixedClassAccount(AccountType.CHECKING),
            )
        )

    @patch("corderohq.personal_finance_handler._PROFILE_TABLE")
    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    def test_creates_excluded_account(self, mock_at: MagicMock, mock_pt: MagicMock) -> None:
        mock_pt.get_person.return_value = {"personId": "p1", "name": "Jordan"}
        mock_at.create_account.return_value = {"accountId": "a1", "name": "529"}
        result = handler(
            _make_event(
                "POST",
                "/api/accounts",
                body={
                    "name": "529",
                    "accountType": "529",
                    "assetClasses": ["us_equity_large_cap"],
                    "owners": ["p1"],
                    "excludedFromNetWorth": True,
                },
            ),
            None,
        )
        assert result["statusCode"] == 201
        assert mock_at.create_account.call_args.args[0].excluded_from_net_worth is True

    @patch("corderohq.personal_finance_handler._PROFILE_TABLE")
    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    def test_creates_with_multiple_owners(self, mock_at: MagicMock, mock_pt: MagicMock) -> None:
        # Every personId resolves; a jointly-held account is just two owners.
        mock_pt.get_person.return_value = {"personId": "x", "name": "Someone"}
        mock_at.create_account.return_value = {"accountId": "a1", "name": "Joint Checking"}
        result = handler(
            _make_event(
                "POST",
                "/api/accounts",
                body={
                    "name": "Joint Checking",
                    "accountType": "checking",
                    "assetClasses": ["cash"],
                    "owners": ["p1", "p2"],
                },
            ),
            None,
        )
        assert result["statusCode"] == 201
        assert mock_at.create_account.call_args.args[0].owners == ["p1", "p2"]

    @patch("corderohq.personal_finance_handler._PROFILE_TABLE")
    def test_unknown_owner_returns_400(self, mock_pt: MagicMock) -> None:
        mock_pt.get_person.return_value = None
        result = handler(
            _make_event(
                "POST",
                "/api/accounts",
                body={
                    "name": "Ghost",
                    "accountType": "checking",
                    "assetClasses": ["cash"],
                    "owners": ["ghost"],
                },
            ),
            None,
        )
        assert result["statusCode"] == 400

    def test_missing_owners_returns_400(self) -> None:
        result = handler(
            _make_event(
                "POST",
                "/api/accounts",
                body={"name": "X", "accountType": "checking", "assetClasses": ["cash"]},
            ),
            None,
        )
        assert result["statusCode"] == 400

    def test_missing_name_returns_400(self) -> None:
        result = handler(
            _make_event("POST", "/api/accounts", body={"accountType": "checking", "owners": ["p1"]}),
            None,
        )
        assert result["statusCode"] == 400

    def test_missing_account_type_returns_400(self) -> None:
        result = handler(
            _make_event("POST", "/api/accounts", body={"name": "X", "owners": ["p1"]}),
            None,
        )
        assert result["statusCode"] == 400

    @patch("corderohq.personal_finance_handler._PROFILE_TABLE")
    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    def test_invalid_asset_class_returns_400(self, mock_at: MagicMock, mock_pt: MagicMock) -> None:
        # A bad asset class fails when the handler converts the request, before any write.
        mock_pt.get_person.return_value = {"personId": "p1", "name": "Jordan"}
        result = handler(
            _make_event(
                "POST",
                "/api/accounts",
                body={
                    "name": "X",
                    "accountType": "brokerage",
                    "assetClasses": ["stocks"],
                    "owners": ["p1"],
                },
            ),
            None,
        )
        assert result["statusCode"] == 400
        mock_at.create_account.assert_not_called()

    @patch("corderohq.personal_finance_handler._PROFILE_TABLE")
    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    def test_non_bool_excluded_flag_returns_400(self, mock_at: MagicMock, mock_pt: MagicMock) -> None:
        mock_pt.get_person.return_value = {"personId": "p1", "name": "Jordan"}
        body = {"name": "X", "accountType": "checking", "owners": ["p1"], "excludedFromNetWorth": "false"}
        result = handler(_make_event("POST", "/api/accounts", body=body), None)
        assert result["statusCode"] == 400
        mock_at.create_account.assert_not_called()


class TestPutAccount:
    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    def test_updates_fields(self, mock_at: MagicMock) -> None:
        mock_at.update.return_value = {"accountId": "a1", "name": "Renamed"}
        result = handler(
            _make_event("PUT", "/api/accounts/a1", body={"name": "Renamed", "sortOrder": 3}),
            None,
        )
        assert result["statusCode"] == 200
        account_id, updates = mock_at.update.call_args.args
        assert account_id == "a1"
        assert updates == {"name": "Renamed", "sortOrder": 3}

    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    def test_updates_excluded_from_net_worth(self, mock_at: MagicMock) -> None:
        mock_at.update.return_value = {"accountId": "a1", "name": "A"}
        result = handler(_make_event("PUT", "/api/accounts/a1", body={"excludedFromNetWorth": True}), None)
        assert result["statusCode"] == 200
        _, updates = mock_at.update.call_args.args
        assert updates == {"excludedFromNetWorth": True}

    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    def test_account_type_and_liability_are_ignored(self, mock_at: MagicMock) -> None:
        # accountType and liability are immutable; the handler drops them from the
        # editable set, so only `name` reaches the table wrapper.
        mock_at.update.return_value = {"accountId": "a1", "name": "X"}
        result = handler(
            _make_event("PUT", "/api/accounts/a1", body={"name": "X", "accountType": "mortgage", "liability": True}),
            None,
        )
        assert result["statusCode"] == 200
        _, updates = mock_at.update.call_args.args
        assert updates == {"name": "X"}

    def test_empty_body_returns_400(self) -> None:
        result = handler(_make_event("PUT", "/api/accounts/a1", body={}), None)
        assert result["statusCode"] == 400

    @patch("corderohq.personal_finance_handler._PROFILE_TABLE")
    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    def test_updates_owners(self, mock_at: MagicMock, mock_pt: MagicMock) -> None:
        mock_pt.get_person.return_value = {"personId": "x", "name": "Someone"}
        mock_at.update.return_value = {"accountId": "a1", "name": "A"}
        result = handler(_make_event("PUT", "/api/accounts/a1", body={"owners": ["p1", "p2"]}), None)
        assert result["statusCode"] == 200
        _, updates = mock_at.update.call_args.args
        assert updates == {"owners": ["p1", "p2"]}

    @patch("corderohq.personal_finance_handler._PROFILE_TABLE")
    def test_unknown_owner_returns_400(self, mock_pt: MagicMock) -> None:
        mock_pt.get_person.return_value = None
        result = handler(_make_event("PUT", "/api/accounts/a1", body={"owners": ["ghost"]}), None)
        assert result["statusCode"] == 400

    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    def test_missing_account_returns_404(self, mock_at: MagicMock) -> None:
        mock_at.update.return_value = None
        result = handler(_make_event("PUT", "/api/accounts/nope", body={"name": "X"}), None)
        assert result["statusCode"] == 404


class TestAccountLifecycle:
    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    def test_deactivate(self, mock_at: MagicMock) -> None:
        mock_at.deactivate.return_value = {"accountId": "a1", "active": False}
        result = handler(_make_event("POST", "/api/accounts/a1/deactivate"), None)
        assert result["statusCode"] == 200
        mock_at.deactivate.assert_called_once_with("a1")

    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    def test_deactivate_missing_returns_404(self, mock_at: MagicMock) -> None:
        mock_at.deactivate.return_value = None
        result = handler(_make_event("POST", "/api/accounts/nope/deactivate"), None)
        assert result["statusCode"] == 404

    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    def test_reactivate(self, mock_at: MagicMock) -> None:
        mock_at.reactivate.return_value = {"accountId": "a1", "active": True}
        result = handler(_make_event("POST", "/api/accounts/a1/reactivate"), None)
        assert result["statusCode"] == 200
        mock_at.reactivate.assert_called_once_with("a1")

    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    def test_reorder(self, mock_at: MagicMock) -> None:
        mock_at.get_all_accounts.return_value = []
        result = handler(_make_event("POST", "/api/accounts/reorder", body={"order": ["a2", "a1"]}), None)
        assert result["statusCode"] == 200
        mock_at.reorder.assert_called_once_with(["a2", "a1"])

    def test_reorder_bad_body_returns_400(self) -> None:
        result = handler(_make_event("POST", "/api/accounts/reorder", body={"order": "nope"}), None)
        assert result["statusCode"] == 400


# --- Net worth snapshots ---


def _acct(account_id: str, asset_classes: list[str]) -> dict[str, object]:
    return {"accountId": account_id, "name": account_id, "active": True, "sortOrder": 0, "assetClasses": asset_classes}


# _FROZEN_NOW is 2026-06-15, so 2026-06 is the current month (recordable) and 2026-07 is the future (rejected).
class TestGetNetWorthMonth:
    @patch("corderohq.personal_finance_handler._NETWORTH_SNAPSHOT_TABLE")
    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    def test_returns_per_class_rows_with_prefill(self, mock_at: MagicMock, mock_st: MagicMock) -> None:
        mock_at.get_all_accounts.return_value = [_acct("a1", ["cash"])]
        mock_st.get_month.return_value = []
        mock_st.scan_all.return_value = [{"yearMonth": "2026-05", "accountId": "a1", "byAssetClass": {"cash": 100}}]
        result = handler(_make_event("GET", "/api/net-worth/2026-06"), None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["yearMonth"] == "2026-06"
        cash = body["rows"][0]["classes"][0]
        assert cash["assetClass"] == "cash"
        assert cash["value"] is None
        assert cash["prefill"] == {"value": 100, "fromYearMonth": "2026-05"}

    def test_future_month_rejected(self) -> None:
        result = handler(_make_event("GET", "/api/net-worth/2026-07"), None)
        assert result["statusCode"] == 400

    def test_malformed_month_rejected(self) -> None:
        result = handler(_make_event("GET", "/api/net-worth/2026-6"), None)
        assert result["statusCode"] == 400


class TestPostNetWorthMonth:
    @patch("corderohq.personal_finance_handler._NETWORTH_SNAPSHOT_TABLE")
    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    def test_upserts_per_class_merge_rows(self, mock_at: MagicMock, mock_st: MagicMock) -> None:
        mock_at.get_all_accounts.return_value = [_acct("a1", ["us_equity_large_cap", "cash"]), _acct("a2", ["bonds"])]
        mock_st.get_month.return_value = []
        mock_st.scan_all.return_value = []
        rows = [
            {
                "accountId": "a1",
                "classes": [
                    {"assetClass": "us_equity_large_cap", "value": 500},
                    {"assetClass": "cash", "value": None},  # clear
                ],
            },
            {"accountId": "a2", "classes": [{"assetClass": "bonds", "value": 400}]},
        ]
        result = handler(_make_event("POST", "/api/net-worth/2026-06", body={"rows": rows}), None)
        assert result["statusCode"] == 200
        mock_st.upsert_month.assert_called_once_with(
            "2026-06",
            [
                {"accountId": "a1", "classes": {"us_equity_large_cap": 500, "cash": None}},
                {"accountId": "a2", "classes": {"bonds": 400}},
            ],
        )

    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    def test_unknown_account_rejected(self, mock_at: MagicMock) -> None:
        mock_at.get_all_accounts.return_value = [_acct("a1", ["cash"])]
        result = handler(
            _make_event(
                "POST",
                "/api/net-worth/2026-06",
                body={"rows": [{"accountId": "ghost", "classes": [{"assetClass": "cash", "value": 1}]}]},
            ),
            None,
        )
        assert result["statusCode"] == 400
        assert json.loads(result["body"])["unknownAccountIds"] == ["ghost"]

    @patch("corderohq.personal_finance_handler._NETWORTH_SNAPSHOT_TABLE")
    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    def test_note_passed_through(self, mock_at: MagicMock, mock_st: MagicMock) -> None:
        mock_at.get_all_accounts.return_value = [_acct("a1", ["cash"])]
        mock_st.get_month.return_value = []
        mock_st.scan_all.return_value = []
        rows = [{"accountId": "a1", "note": "quarterly bonus", "classes": [{"assetClass": "cash", "value": 500}]}]
        result = handler(_make_event("POST", "/api/net-worth/2026-06", body={"rows": rows}), None)
        assert result["statusCode"] == 200
        mock_st.upsert_month.assert_called_once_with(
            "2026-06", [{"accountId": "a1", "classes": {"cash": 500}, "note": "quarterly bonus"}]
        )

    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    def test_invalid_asset_class_rejected(self, mock_at: MagicMock) -> None:
        mock_at.get_all_accounts.return_value = [_acct("a1", ["cash"])]
        result = handler(
            _make_event(
                "POST",
                "/api/net-worth/2026-06",
                body={"rows": [{"accountId": "a1", "classes": [{"assetClass": "stonks", "value": 1}]}]},
            ),
            None,
        )
        assert result["statusCode"] == 400

    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    def test_negative_value_rejected(self, mock_at: MagicMock) -> None:
        mock_at.get_all_accounts.return_value = [_acct("a1", ["cash"])]
        result = handler(
            _make_event(
                "POST",
                "/api/net-worth/2026-06",
                body={"rows": [{"accountId": "a1", "classes": [{"assetClass": "cash", "value": -5}]}]},
            ),
            None,
        )
        assert result["statusCode"] == 400

    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    def test_non_integer_value_rejected(self, mock_at: MagicMock) -> None:
        mock_at.get_all_accounts.return_value = [_acct("a1", ["cash"])]
        result = handler(
            _make_event(
                "POST",
                "/api/net-worth/2026-06",
                body={"rows": [{"accountId": "a1", "classes": [{"assetClass": "cash", "value": 5.5}]}]},
            ),
            None,
        )
        assert result["statusCode"] == 400

    def test_missing_rows_rejected(self) -> None:
        result = handler(_make_event("POST", "/api/net-worth/2026-06", body={}), None)
        assert result["statusCode"] == 400

    def test_future_month_rejected(self) -> None:
        result = handler(_make_event("POST", "/api/net-worth/2026-07", body={"rows": []}), None)
        assert result["statusCode"] == 400


class TestGetNetWorthHistory:
    @patch("corderohq.personal_finance_handler._NETWORTH_SNAPSHOT_TABLE")
    @patch("corderohq.personal_finance_handler._ACCOUNT_TABLE")
    def test_returns_history_shape(self, mock_at: MagicMock, mock_st: MagicMock) -> None:
        mock_at.get_all_accounts.return_value = [
            {"accountId": "a1", "name": "Checking", "active": True, "liability": False, "sortOrder": 0},
        ]
        mock_st.scan_all.return_value = [{"yearMonth": "2026-05", "accountId": "a1", "byAssetClass": {"cash": 1000}}]
        result = handler(_make_event("GET", "/api/net-worth/history"), None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["months"] == ["2026-05"]
        assert body["totals"]["2026-05"]["netWorth"] == 1000
        # /history must not be parsed as a {YYYY-MM} — the month handler never runs.
        mock_st.get_month.assert_not_called()
