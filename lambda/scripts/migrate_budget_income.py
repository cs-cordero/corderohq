import argparse
from typing import Any

import boto3

_LIABILITY_TYPES = {"mortgage", "other_liability"}
_OLD_CLASS = "other"
_NEW_CLASS = "debt"


def migrate(stage: str, apply: bool, to_credit_card: list[str]) -> None:
    """Run each step of the migration on one stage, and print each change. Write only when `apply` is True."""

    dynamodb = boto3.resource("dynamodb")
    liability_ids, account_writes, account_count = _migrate_accounts(
        dynamodb.Table(f"Account-{stage}"), apply, to_credit_card
    )
    snapshot_writes, snapshot_count = _migrate_snapshots(
        dynamodb.Table(f"NetWorthSnapshot-{stage}"), apply, liability_ids
    )
    budget_writes, budget_count = _migrate_budget_rows(dynamodb.Table(f"Budget-{stage}"), apply)

    mode = "APPLIED" if apply else "DRY RUN (pass --apply to write)"
    print(
        f"\n[{stage}] {mode}: {account_writes} of {account_count} account(s), {snapshot_writes} of {snapshot_count} "
        f"snapshot row(s), and {budget_writes} of {budget_count} budget row(s) needed migration."
    )


def main() -> None:
    """Migrate the stored data of one stage to the budget income feature.

    1. `Account-{stage}`: each `mortgage` or `other_liability` account gets `assetClasses = ["debt"]`. Each account
       named with `--to-credit-card` gets `accountType = "credit_card"`, `assetClasses = ["debt"]`, and no
       `loanTerms`. It must be an `other_liability` account now.
    2. `NetWorthSnapshot-{stage}`: each row of those accounts moves its value from `byAssetClass.other` to
       `byAssetClass.debt`.
    3. `Budget-{stage}`: each row renames `amount` to `carriedAmount` and `onlyAmount` to `pinnedAmount`. A null
       `amount` becomes 0.

    Each step skips the data that it migrated already, so the script is safe to run again. It does a dry run by
    default. Run it on dev first, then on prod, right after the budget income code is deployed to that stage.

    Usage:
        cd lambda
        uv run python scripts/migrate_budget_income.py --stage dev
        uv run python scripts/migrate_budget_income.py --stage dev --apply --to-credit-card <accountId>
    """

    parser = argparse.ArgumentParser(description=main.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", required=True, choices=["dev", "prod"])
    parser.add_argument("--apply", action="store_true", help="write changes (default is a dry run)")
    parser.add_argument(
        "--to-credit-card",
        action="append",
        default=[],
        metavar="ACCOUNT_ID",
        help="an other_liability account to change to credit_card (repeat for each card)",
    )
    args = parser.parse_args()
    migrate(args.stage, args.apply, args.to_credit_card)


def _migrate_accounts(table: Any, apply: bool, to_credit_card: list[str]) -> tuple[set[str], int, int]:
    """Move each liability to the `debt` class. Return the liability ids, the count of writes, and of accounts."""

    accounts = {str(a["accountId"]): a for a in _scan_all(table)}
    for account_id in to_credit_card:
        account = accounts.get(account_id)
        if account is None:
            raise SystemExit(f"--to-credit-card {account_id}: no such account")
        if account.get("accountType") not in ("other_liability", "credit_card"):
            raise SystemExit(
                f"--to-credit-card {account_id} ({account.get('name')}): the type is "
                f"{account.get('accountType')}, not other_liability"
            )

    liability_ids: set[str] = set()
    writes = 0
    for account_id, account in accounts.items():
        account_type = account.get("accountType")
        becomes_card = account_id in to_credit_card
        if account_type not in _LIABILITY_TYPES and account_type != "credit_card" and not becomes_card:
            continue
        liability_ids.add(account_id)

        if becomes_card and account_type != "credit_card":
            writes += 1
            print(f"  account {account_id} ({account.get('name')}): {account_type} → credit_card, classes → [debt]")
            if apply:
                table.update_item(
                    Key={"accountId": account_id},
                    UpdateExpression="SET accountType = :t, assetClasses = :ac REMOVE loanTerms",
                    ExpressionAttributeValues={":t": "credit_card", ":ac": [_NEW_CLASS]},
                )
        elif account.get("assetClasses") != [_NEW_CLASS]:
            writes += 1
            print(f"  account {account_id} ({account.get('name')}): {account.get('assetClasses')} → [debt]")
            if apply:
                table.update_item(
                    Key={"accountId": account_id},
                    UpdateExpression="SET assetClasses = :ac",
                    ExpressionAttributeValues={":ac": [_NEW_CLASS]},
                )
    return liability_ids, writes, len(accounts)


def _migrate_snapshots(table: Any, apply: bool, liability_ids: set[str]) -> tuple[int, int]:
    """Move the value of each liability from `other` to `debt`. Return the count of writes, and of rows."""

    snapshots = _scan_all(table)
    writes = 0
    for snap in snapshots:
        account_id = str(snap["accountId"])
        if account_id not in liability_ids:
            continue
        by_class = dict(snap.get("byAssetClass") or {})
        if _OLD_CLASS not in by_class:
            continue
        if _NEW_CLASS in by_class:
            print(f"  ! snapshot {snap['yearMonth']}/{account_id} has both 'other' and 'debt'; skipping")
            continue
        by_class[_NEW_CLASS] = by_class.pop(_OLD_CLASS)
        writes += 1
        print(f"  snapshot {snap['yearMonth']}/{account_id}: other → debt ({by_class[_NEW_CLASS]})")
        if apply:
            table.update_item(
                Key={"yearMonth": snap["yearMonth"], "accountId": account_id},
                UpdateExpression="SET byAssetClass = :m",
                ExpressionAttributeValues={":m": by_class},
            )
    return writes, len(snapshots)


def _migrate_budget_rows(table: Any, apply: bool) -> tuple[int, int]:
    """Rename the amounts of each Budget row. Return the count of writes, and of rows."""

    rows = _scan_all(table)
    writes = 0
    for row in rows:
        if "amount" not in row and "onlyAmount" not in row:
            continue
        item = {key: value for key, value in row.items() if key not in ("amount", "onlyAmount")}
        if "amount" in row:
            item["carriedAmount"] = 0 if row["amount"] is None else row["amount"]
        if row.get("onlyAmount") is not None:
            item["pinnedAmount"] = row["onlyAmount"]
        if "carriedAmount" not in item and "pinnedAmount" not in item:
            print(f"  ! budget row {row['yearMonth']}/{row['categoryId']} has no amount at all; skipping")
            continue
        writes += 1
        print(
            f"  budget row {row['yearMonth']}/{row['categoryId']}: carriedAmount={item.get('carriedAmount')}, "
            f"pinnedAmount={item.get('pinnedAmount')}"
        )
        if apply:
            table.put_item(Item=item)
    return writes, len(rows)


def _scan_all(table: Any) -> list[dict[str, Any]]:
    """Return every item of a table, through each page of the scan."""

    items: list[dict[str, Any]] = []
    response = table.scan()
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))
    return items


if __name__ == "__main__":
    main()
