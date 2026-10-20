"""The DynamoDB table classes of the personal finance app. Each table has its own file."""

from corderohq.aws.dynamodb.account import AccountTable
from corderohq.aws.dynamodb.budget import BudgetCarryStore, BudgetTable
from corderohq.aws.dynamodb.budget_audit_log import BudgetAuditLogTable
from corderohq.aws.dynamodb.category import CategoryTable, active_category_ids
from corderohq.aws.dynamodb.category_group import CategoryGroupTable
from corderohq.aws.dynamodb.income_plan import IncomePlanCarryStore, IncomePlanTable
from corderohq.aws.dynamodb.income_source import IncomeSourceTable, active_income_source_ids
from corderohq.aws.dynamodb.income_summary import compute_income
from corderohq.aws.dynamodb.net_worth_snapshot import NetWorthSnapshotTable
from corderohq.aws.dynamodb.profile import ProfileTable
from corderohq.aws.dynamodb.summary import compute_summary
from corderohq.aws.dynamodb.transactions import TransactionsTable
from corderohq.aws.dynamodb.util import current_year_month

__all__ = [
    "AccountTable",
    "BudgetAuditLogTable",
    "BudgetCarryStore",
    "BudgetTable",
    "CategoryGroupTable",
    "CategoryTable",
    "IncomePlanCarryStore",
    "IncomePlanTable",
    "IncomeSourceTable",
    "NetWorthSnapshotTable",
    "ProfileTable",
    "TransactionsTable",
    "active_category_ids",
    "active_income_source_ids",
    "compute_income",
    "compute_summary",
    "current_year_month",
]
