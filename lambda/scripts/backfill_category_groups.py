import argparse
from datetime import UTC, datetime
from typing import Any

import boto3
from ulid import ULID


def _scan_all(table: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    response = table.scan()
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))
    return items


def backfill(stage: str, dry_run: bool) -> None:
    dynamodb = boto3.resource("dynamodb")
    group_table = dynamodb.Table(f"CategoryGroup-{stage}")
    category_table = dynamodb.Table(f"Category-{stage}")

    existing_groups = _scan_all(group_table)
    categories = _scan_all(category_table)

    cats_needing_group = [c for c in categories if "groupId" not in c]
    unassigned = next((g for g in existing_groups if g.get("system")), None)
    print(
        f"[{stage}] {len(existing_groups)} groups, {len(categories)} categories "
        f"({len(cats_needing_group)} orphaned, "
        f"Unassigned sentinel {'present' if unassigned else 'missing'})"
    )

    if unassigned and not cats_needing_group:
        print(f"[{stage}] nothing to do")
        return

    now = datetime.now(tz=UTC).isoformat()

    # 1. Ensure the "Unassigned" sentinel exists. The backend blocks rename/delete on
    # system groups, and `get_all_category_groups()` always sorts them last.
    if unassigned is None:
        unassigned = {
            "groupId": str(ULID()),
            "name": "Unassigned",
            "order": 9999,  # cosmetic; the backend pushes system groups last regardless
            "createdAt": now,
            "updatedAt": now,
            "system": True,
        }
        if dry_run:
            print(f"[{stage}] would create Unassigned sentinel group: {unassigned['groupId']}")
        else:
            group_table.put_item(Item=unassigned)
            print(f"[{stage}] created Unassigned sentinel group: {unassigned['groupId']}")

    if not cats_needing_group:
        return

    # 2. Sweep any orphan categories (no `groupId`) into Unassigned. These shouldn't
    # appear organically — the API requires `groupId` at create time — but the
    # sweep guards against legacy data or direct DB writes.
    cats_needing_group.sort(key=lambda c: c["name"].lower())
    for index, cat in enumerate(cats_needing_group):
        if dry_run:
            print(
                f"  would assign orphan {cat['name']} ({cat['categoryId']}) "
                f"→ Unassigned {unassigned['groupId']} order {index}"
            )
            continue
        category_table.update_item(
            Key={"categoryId": cat["categoryId"]},
            UpdateExpression="SET groupId = :g, orderInGroup = :o, updatedAt = :now",
            ExpressionAttributeValues={":g": unassigned["groupId"], ":o": index, ":now": now},
        )

    if not dry_run:
        print(f"[{stage}] assigned {len(cats_needing_group)} orphan categories to Unassigned")


def main() -> None:
    """Make sure that the "Unassigned" group exists, and move each category with no groupId into it.

    The script is safe to run again.

    Usage:
        uv run python lambda/scripts/backfill_category_groups.py --stage dev
        uv run python lambda/scripts/backfill_category_groups.py --stage prod
    """

    parser = argparse.ArgumentParser(description=main.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", required=True, choices=("dev", "prod"))
    parser.add_argument("--dry-run", action="store_true", help="Print what would be written without writing")
    args = parser.parse_args()
    backfill(args.stage, args.dry_run)


if __name__ == "__main__":
    main()
