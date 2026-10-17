"""Bulk-import historical net-worth snapshots from a wide CSV.

Backfills per-(account, asset-class, month) values into an existing stage's
`NetWorthSnapshot-{stage}` table. Accounts must already exist (matched by name);
this script never creates or edits accounts. Dollar amounts become integer
millionths; blank cells write nothing (sparse by design).

CSV shape — (Account, AssetClass) rows, month-year columns:

    Account,AssetClass,2019-01,2019-02,...,2026-09
    Chase Checking,Cash,5000.00,5100.00,...
    Roth IRA,US Equity Large Cap,,,10000,11000,...
    Roth IRA,US Equity Small Cap,,,2000,2100,...
    Roth IRA,Cash,,,150,150,...
    Mortgage,,412000,411000,...

- AssetClass accepts a friendly label ("US Equity Large Cap") or the raw code
  ("us_equity_large_cap"). Leave it blank for single-class accounts (checking →
  cash, mortgage → debt) — it defaults to the account's sole class; it is required
  for multi-class accounts.
- One DynamoDB item is written per (month, account): the asset-class cells for that
  account/month become its `byAssetClass` map. Re-running overwrites those balances
  (idempotent) while preserving any existing note on the row.

The CSV holds real account names and balances — keep it OUT of the repo (pass a
path under your home dir; the repo is public).

Usage:
    uv run python lambda/scripts/import_networth_csv.py --stage dev  --file ~/networth-history.csv
    uv run python lambda/scripts/import_networth_csv.py --stage dev  --file ~/networth-history.csv --apply
    uv run python lambda/scripts/import_networth_csv.py --stage prod --file ~/networth-history.csv --apply
"""

import argparse
import csv
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import boto3

from corderohq.networth.models import AssetClass, validate_asset_class

_BUDGET_TZ = ZoneInfo("America/New_York")
# Friendly labels → enum codes (plus each code maps to itself), matched case-insensitively.
_LABELS = {
    "cash": "cash",
    "us equity large cap": "us_equity_large_cap",
    "us equity small cap": "us_equity_small_cap",
    "international equity": "intl_equity",
    "intl equity": "intl_equity",
    "bonds": "bonds",
    "fixed income": "fixed_income",
    "real estate": "real_estate",
    "target date": "target_date",
    "other": "other",
    "debt": "debt",
}
_CODE_ALIASES = {**_LABELS, **{c.value: c.value for c in AssetClass}}


def _current_year_month() -> str:
    et = datetime.now(tz=UTC).astimezone(_BUDGET_TZ)
    return f"{et.year:04d}-{et.month:02d}"


def _scan_all(table: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    response = table.scan()
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))
    return items


def _parse_millionths(raw: str) -> int:
    """Dollar string → integer millionths. `$-`, `-`, or empty read as 0 (accounting
    zero). Negatives (incl. accounting parentheses like `$(12.34)`) raise — record
    liabilities as positive balances; assets can't be negative."""
    cleaned = raw.strip().replace("$", "").replace(",", "").replace(" ", "")
    if cleaned in ("", "-"):
        return 0
    value = round(float(cleaned) * 1_000_000)
    if value < 0:
        raise ValueError(f"value {raw!r} is negative — enter liabilities as a positive balance")
    return value


def _resolve_class(raw: str, account_classes: list[str], account_name: str) -> str:
    raw = raw.strip()
    if not raw:
        if len(account_classes) == 1:
            return account_classes[0]
        raise ValueError(
            f"account {account_name!r} holds {len(account_classes)} asset classes; "
            "AssetClass column is required (blank only allowed for single-class accounts)"
        )
    code = _CODE_ALIASES.get(raw.lower())
    if code is None:
        raise ValueError(f"unknown asset class {raw!r} for account {account_name!r}")
    return validate_asset_class(code)


def run(stage: str, file_path: str, apply: bool) -> None:
    dynamodb = boto3.resource("dynamodb")
    account_table = dynamodb.Table(f"Account-{stage}")
    snapshot_table = dynamodb.Table(f"NetWorthSnapshot-{stage}")

    accounts = _scan_all(account_table)
    by_name = {str(a["name"]).strip().lower(): a for a in accounts}
    current_ym = _current_year_month()

    # utf-8-sig strips a leading BOM that spreadsheet exports often add.
    with open(file_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        data_rows = [r for r in reader if any(cell.strip() for cell in r)]

    def _norm(h: str) -> str:
        return h.strip().lower().replace(" ", "")

    if len(header) < 3 or _norm(header[0]) not in ("account", "accountname") or _norm(header[1]) != "assetclass":
        raise SystemExit("CSV header must start with: Account,AssetClass,<YYYY-MM>,<YYYY-MM>,... (spaces OK)")

    months = [h.strip() for h in header[2:]]
    errors: list[str] = []
    for i, ym in enumerate(months):
        if not (len(ym) == 7 and ym[4] == "-" and ym[:4].isdigit() and ym[5:].isdigit()):
            errors.append(f"header column {i + 3}: {ym!r} is not a YYYY-MM month")
        elif ym > current_ym:
            errors.append(f"month {ym} is in the future (current is {current_ym})")

    # (yearMonth, accountId) -> {assetClass: millionths}
    grouped: dict[tuple[str, str], dict[str, int]] = {}
    warnings: list[str] = []
    for rownum, row in enumerate(data_rows, start=2):
        name = (row[0] if len(row) > 0 else "").strip()
        account = by_name.get(name.lower())
        if account is None:
            errors.append(f"row {rownum}: unknown account {name!r}")
            continue
        account_classes = list(account.get("assetClasses", []))
        try:
            asset_class = _resolve_class(row[1] if len(row) > 1 else "", account_classes, name)
        except ValueError as e:
            errors.append(f"row {rownum}: {e}")
            continue
        if asset_class not in account_classes:
            warnings.append(
                f"row {rownum}: {name!r} — asset class {asset_class!r} is not in the account's current set "
                f"{account_classes}; importing anyway (historical)"
            )
        for col, ym in enumerate(months):
            cell = row[col + 2] if len(row) > col + 2 else ""
            if not cell.strip():
                continue
            try:
                millionths = _parse_millionths(cell)
            except ValueError as e:
                errors.append(f"row {rownum}, month {ym}: {e}")
                continue
            if millionths == 0:  # $- / 0 → no snapshot (sparse; contributes 0 to totals anyway)
                continue
            grouped.setdefault((ym, account["accountId"]), {})[asset_class] = millionths

    print(f"[{stage}] {len(accounts)} accounts, {len(data_rows)} CSV rows, {len(months)} month columns")
    print(f"[{stage}] {len(grouped)} (account, month) snapshot rows to write, {len(warnings)} warning(s)")
    for w in warnings:
        print(f"  warning: {w}")
    if errors:
        print(f"\n[{stage}] {len(errors)} ERROR(S) — nothing written:")
        for e in errors:
            print(f"  - {e}")
        raise SystemExit(1)

    if not apply:
        print(f"\n[{stage}] DRY RUN — re-run with --apply to write.")
        return

    now = datetime.now(tz=UTC).isoformat()
    for (ym, account_id), by_class in grouped.items():
        existing = snapshot_table.get_item(Key={"yearMonth": ym, "accountId": account_id}).get("Item") or {}
        item: dict[str, Any] = {
            "yearMonth": ym,
            "accountId": account_id,
            "byAssetClass": by_class,
            "updatedAt": now,
        }
        if existing.get("note"):  # preserve a note already recorded for this row
            item["note"] = existing["note"]
        snapshot_table.put_item(Item=item)
    print(f"\n[{stage}] APPLIED: wrote {len(grouped)} snapshot rows.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=["dev", "prod"])
    parser.add_argument("--file", required=True, help="path to the wide CSV (kept outside the repo)")
    parser.add_argument("--apply", action="store_true", help="write changes (default is a dry run)")
    args = parser.parse_args()
    run(args.stage, args.file, args.apply)


if __name__ == "__main__":
    main()
