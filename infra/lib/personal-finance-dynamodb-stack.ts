import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import type { Construct } from "constructs";
import type { Stage } from "./constants";

interface PersonalFinanceDynamoDbStackProps extends cdk.StackProps {
    readonly stage: Stage;
}

/**
 * DynamoDB tables for the personal finance app.
 *
 * AWS resources:
 * - DynamoDB table: CategoryGroup-{stage} (PK: groupId)
 * - DynamoDB table: Category-{stage} (PK: categoryId)
 * - DynamoDB table: Budget-{stage} (PK: yearMonth, SK: categoryId)
 * - DynamoDB table: BudgetAuditLog-{stage} (PK: entityType [always "AUDIT"], SK: sortId [ULID])
 * - DynamoDB table: Transactions-{stage} (PK: yearMonth, SK: sortId)
 * - DynamoDB table: IncomeSource-{stage} (PK: incomeSourceId [ULID])
 * - DynamoDB table: IncomePlan-{stage} (PK: yearMonth, SK: lineId ["SRC#{incomeSourceId}" or "ONE#{ULID}"])
 * - DynamoDB table: Profile-{stage} (PK: householdId [always "HOUSEHOLD"], SK: personId [ULID])
 * - DynamoDB table: Account-{stage} (PK: accountId [ULID])
 * - DynamoDB table: NetWorthSnapshot-{stage} (PK: yearMonth, SK: accountId)
 */
export class PersonalFinanceDynamoDbStack extends cdk.Stack {
    readonly categoryGroupTable: dynamodb.Table;
    readonly categoryTable: dynamodb.Table;
    readonly budgetTable: dynamodb.Table;
    readonly budgetAuditLogTable: dynamodb.Table;
    readonly transactionsTable: dynamodb.Table;
    readonly incomeSourceTable: dynamodb.Table;
    readonly incomePlanTable: dynamodb.Table;
    readonly profileTable: dynamodb.Table;
    readonly accountTable: dynamodb.Table;
    readonly netWorthSnapshotTable: dynamodb.Table;

    constructor(scope: Construct, id: string, props: PersonalFinanceDynamoDbStackProps) {
        super(scope, id, props);

        const { stage } = props;
        // A prod table stays when the stack is deleted. A `cdk destroy` by mistake, or a lift
        // of the termination protection, therefore cannot remove the live data. A dev table is
        // deleted, so that we can change it freely. A change to a schema that needs a new
        // table, such as the CP3 change to the audit-log PK, needs a migration script. Run
        // that script one time when the change reaches prod.
        const removalPolicy = stage === "prod" ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY;

        cdk.Tags.of(this).add("project", "personal-finance");
        cdk.Tags.of(this).add("stage", stage);

        this.categoryGroupTable = new dynamodb.Table(this, "CategoryGroupTable", {
            tableName: `CategoryGroup-${stage}`,
            partitionKey: { name: "groupId", type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
            removalPolicy,
        });

        this.categoryTable = new dynamodb.Table(this, "CategoryTable", {
            tableName: `Category-${stage}`,
            partitionKey: { name: "categoryId", type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
            removalPolicy,
        });

        this.budgetTable = new dynamodb.Table(this, "BudgetTable", {
            tableName: `Budget-${stage}`,
            partitionKey: { name: "yearMonth", type: dynamodb.AttributeType.STRING },
            sortKey: { name: "categoryId", type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
            removalPolicy,
        });

        this.budgetAuditLogTable = new dynamodb.Table(this, "BudgetAuditLogTable", {
            tableName: `BudgetAuditLog-${stage}`,
            // One partition only. The PK is always the string "AUDIT", and the SK is a ULID. A
            // read of the most recent N entries is then one Query, with ScanIndexForward=false.
            partitionKey: { name: "entityType", type: dynamodb.AttributeType.STRING },
            sortKey: { name: "sortId", type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
            removalPolicy,
        });

        this.transactionsTable = new dynamodb.Table(this, "TransactionsTable", {
            tableName: `Transactions-${stage}`,
            partitionKey: { name: "yearMonth", type: dynamodb.AttributeType.STRING },
            sortKey: { name: "sortId", type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
            removalPolicy,
        });

        // The tables for the income plan. An income source is one job of one person. The plan
        // holds one paycheck row ("SRC#{incomeSourceId}") for each source in each dense month,
        // and the one-time income lines ("ONE#{ULID}") of each month.
        this.incomeSourceTable = new dynamodb.Table(this, "IncomeSourceTable", {
            tableName: `IncomeSource-${stage}`,
            partitionKey: { name: "incomeSourceId", type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
            removalPolicy,
        });

        this.incomePlanTable = new dynamodb.Table(this, "IncomePlanTable", {
            tableName: `IncomePlan-${stage}`,
            partitionKey: { name: "yearMonth", type: dynamodb.AttributeType.STRING },
            sortKey: { name: "lineId", type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
            removalPolicy,
        });

        // The tables for the net worth record.
        this.profileTable = new dynamodb.Table(this, "ProfileTable", {
            tableName: `Profile-${stage}`,
            // One item collection. The PK is always "HOUSEHOLD", and there is one item for each
            // person, with a ULID as the SK. One Query then returns the whole household.
            partitionKey: { name: "householdId", type: dynamodb.AttributeType.STRING },
            sortKey: { name: "personId", type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
            removalPolicy,
        });

        this.accountTable = new dynamodb.Table(this, "AccountTable", {
            tableName: `Account-${stage}`,
            partitionKey: { name: "accountId", type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
            removalPolicy,
        });

        this.netWorthSnapshotTable = new dynamodb.Table(this, "NetWorthSnapshotTable", {
            tableName: `NetWorthSnapshot-${stage}`,
            partitionKey: { name: "yearMonth", type: dynamodb.AttributeType.STRING },
            sortKey: { name: "accountId", type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
            removalPolicy,
        });
    }
}
