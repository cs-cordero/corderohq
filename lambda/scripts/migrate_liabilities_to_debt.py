"""One-shot migration: move every liability to the `debt` asset class, and make credit card accounts.

Before this change, the liability types (`mortgage`, `other_liability`) had the fixed asset class
`other`. They now have `debt`, and the new type `credit_card` has `debt` too. This script moves
the stored data to match:

1. `Account-{stage}`: each `mortgage` or `other_liability` account gets `assetClasses = ["debt"]`.
2. `Account-{stage}`: each account named with `--to-credit-card` gets `accountType = "credit_card"`,
   `assetClasses = ["debt"]`, and no `loanTerms`. It must be an `other_liability` account now.
3. `NetWorthSnapshot-{stage}`: each row of those accounts moves its value from
   `byAssetClass.other` to `byAssetClass.debt`.

Idempotent: a row that already has `debt` and no `other` is skipped, so it is safe to run again.
Dry run by default. Run it on dev first, then on prod, after the code that knows `debt` is
deployed to that stage.

Usage:
    uv run python lambda/scripts/migrate_liabilities_to_debt.py --stage dev
    uv run python lambda/scripts/migrate_liabilities_to_debt.py --stage dev --apply --to-credit-card <accountId>
"""

import argparse
from typing import Any

import boto3

_LIABILITY_TYPES = {"mortgage", "other_liability"}
_OLD_CLASS = "other"
_NEW_CLASS = "debt"


def _scan_all(table: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    response = table.scan()
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))
    return items


def migrate(stage: str, apply: bool, to_credit_card: list[str]) -> None:
    dynamodb = boto3.resource("dynamodb")
    account_table = dynamodb.Table(f"Account-{stage}")
    snapshot_table = dynamodb.Table(f"NetWorthSnapshot-{stage}")

    accounts = {str(a["accountId"]): a for a in _scan_all(account_table)}

    for account_id in to_credit_card:
        account = accounts.get(account_id)
        if account is None:
            raise SystemExit(f"--to-credit-card {account_id}: no such account")
        if account.get("accountType") not in ("other_liability", "credit_card"):
            raise SystemExit(
                f"--to-credit-card {account_id} ({account.get('name')}): the type is "
                f"{account.get('accountType')}, not other_liability"
            )

    # --- Accounts ---
    liability_ids: set[str] = set()
    account_writes = 0
    for account_id, account in accounts.items():
        account_type = account.get("accountType")
        becomes_card = account_id in to_credit_card
        if account_type not in _LIABILITY_TYPES and account_type != "credit_card" and not becomes_card:
            continue
        liability_ids.add(account_id)

        if becomes_card and account_type != "credit_card":
            account_writes += 1
            print(f"  account {account_id} ({account.get('name')}): {account_type} → credit_card, classes → [debt]")
            if apply:
                account_table.update_item(
                    Key={"accountId": account_id},
                    UpdateExpression="SET accountType = :t, assetClasses = :ac REMOVE loanTerms",
                    ExpressionAttributeValues={":t": "credit_card", ":ac": [_NEW_CLASS]},
                )
        elif account.get("assetClasses") != [_NEW_CLASS]:
            account_writes += 1
            print(f"  account {account_id} ({account.get('name')}): {account.get('assetClasses')} → [debt]")
            if apply:
                account_table.update_item(
                    Key={"accountId": account_id},
                    UpdateExpression="SET assetClasses = :ac",
                    ExpressionAttributeValues={":ac": [_NEW_CLASS]},
                )

    # --- Snapshots ---
    snapshot_writes = 0
    snapshots = _scan_all(snapshot_table)
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
        snapshot_writes += 1
        print(f"  snapshot {snap['yearMonth']}/{account_id}: other → debt ({by_class[_NEW_CLASS]})")
        if apply:
            snapshot_table.update_item(
                Key={"yearMonth": snap["yearMonth"], "accountId": account_id},
                UpdateExpression="SET byAssetClass = :m",
                ExpressionAttributeValues={":m": by_class},
            )

    mode = "APPLIED" if apply else "DRY RUN (pass --apply to write)"
    print(
        f"\n[{stage}] {mode}: {account_writes} account(s) and {snapshot_writes} snapshot row(s) "
        f"needed migration (of {len(accounts)} accounts, {len(snapshots)} snapshots)."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
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


if __name__ == "__main__":
    main()
