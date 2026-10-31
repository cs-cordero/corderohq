from unittest.mock import MagicMock

from corderohq.aws.dynamodb import AuditAction, BudgetAuditLogTable


def _make_table() -> tuple[BudgetAuditLogTable, MagicMock]:
    """Build a BudgetAuditLogTable backed by a MagicMock DynamoDB resource.

    Returns (table, mock) so tests can assert on the mock directly instead of
    going through `mock.*` — which ty rejects because the class
    annotates `_table` with the real boto3 Table type.
    """

    at = BudgetAuditLogTable.__new__(BudgetAuditLogTable)
    mock = MagicMock()
    at._table = mock
    return at, mock


def _user(email: str = "test@example.com") -> dict[str, str]:
    return {"sub": "sub-123", "email": email, "username": email}


class TestWriteEntry:
    def test_writes_entry_with_all_fields(self) -> None:
        table, mock = _make_table()
        result = table.write_entry(
            effective_year_month="2026-03",
            category_id="cat1",
            action=AuditAction.UPDATE,
            explanation="Adjusted grocery budget",
            user=_user(),
            changes={"amount": {"before": 500, "after": 600}},
        )

        assert result["entityType"] == "AUDIT"
        assert result["effectiveYearMonth"] == "2026-03"
        assert result["categoryId"] == "cat1"
        assert result["action"] == "UPDATE"
        assert result["explanation"] == "Adjusted grocery budget"
        assert result["user"] == {"sub": "sub-123", "email": "test@example.com", "username": "test@example.com"}
        assert result["override"] is False
        assert "sortId" in result
        assert "changedAt" in result
        assert "changedAtYearMonth" not in result
        mock.put_item.assert_called_once()

    def test_records_override_when_set(self) -> None:
        table, _mock = _make_table()
        result = table.write_entry(
            effective_year_month="2026-03",
            category_id="cat1",
            action=AuditAction.UPDATE,
            explanation="Backdated correction",
            user=_user("admin@example.com"),
            changes={"amount": {"before": 500, "after": 600}},
            override=True,
        )
        assert result["override"] is True

    def test_allows_null_effective_year_month(self) -> None:
        # CATEGORY_HARD_DELETE-style entries aren't scoped to a single month.
        table, _mock = _make_table()
        result = table.write_entry(
            effective_year_month=None,
            category_id="cat1",
            action=AuditAction.CATEGORY_HARD_DELETE,
            explanation="Spelling error in category name",
            user=_user("admin@example.com"),
            changes={"budgetRowsDeleted": 0, "transactionsDeleted": 0, "name": "Old"},
            override=True,
        )
        assert result["effectiveYearMonth"] is None


class TestReadRecentEntries:
    def test_single_query_against_audit_partition(self) -> None:
        table, mock = _make_table()
        mock.query.return_value = {
            "Items": [
                {"sortId": "01BBB", "action": "UPDATE"},
                {"sortId": "01AAA", "action": "CREATE"},
            ]
        }

        result = table.read_recent_entries(limit=10)

        assert len(result) == 2
        # DynamoDB returns items in DESC order due to ScanIndexForward=False.
        assert result[0]["sortId"] == "01BBB"
        assert result[1]["sortId"] == "01AAA"

        mock.query.assert_called_once()
        call_kwargs = mock.query.call_args.kwargs
        assert call_kwargs["KeyConditionExpression"] == "entityType = :pk"
        assert call_kwargs["ExpressionAttributeValues"] == {":pk": "AUDIT"}
        assert call_kwargs["ScanIndexForward"] is False
        assert call_kwargs["Limit"] == 10

    def test_passes_limit_through_to_dynamodb(self) -> None:
        table, mock = _make_table()
        mock.query.return_value = {"Items": []}

        table.read_recent_entries(limit=3)

        assert mock.query.call_args.kwargs["Limit"] == 3
