import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { PersonalFinanceDynamoDbStack } from "../lib/personal-finance-dynamodb-stack";

function createTestTemplate(stage: "dev" | "prod"): Template {
    const app = new cdk.App();
    const stack = new PersonalFinanceDynamoDbStack(app, "TestPersonalFinanceDynamoDbStack", {
        env: { account: "123456789012", region: "us-east-1" },
        stage,
    });
    return Template.fromStack(stack);
}

describe("PersonalFinanceDynamoDbStack", () => {
    describe("prod stage", () => {
        let template: Template;

        beforeAll(() => {
            template = createTestTemplate("prod");
        });

        test("creates ten DynamoDB tables", () => {
            template.resourceCountIs("AWS::DynamoDB::Table", 10);
        });

        test("Category table has correct key schema", () => {
            template.hasResourceProperties("AWS::DynamoDB::Table", {
                TableName: "Category-prod",
                KeySchema: [{ AttributeName: "categoryId", KeyType: "HASH" }],
            });
        });

        test("Budget table has correct key schema", () => {
            template.hasResourceProperties("AWS::DynamoDB::Table", {
                TableName: "Budget-prod",
                KeySchema: [
                    { AttributeName: "yearMonth", KeyType: "HASH" },
                    { AttributeName: "categoryId", KeyType: "RANGE" },
                ],
            });
        });

        test("BudgetAuditLog table has correct key schema", () => {
            template.hasResourceProperties("AWS::DynamoDB::Table", {
                TableName: "BudgetAuditLog-prod",
                KeySchema: [
                    { AttributeName: "entityType", KeyType: "HASH" },
                    { AttributeName: "sortId", KeyType: "RANGE" },
                ],
            });
        });

        test("Transactions table has correct key schema", () => {
            template.hasResourceProperties("AWS::DynamoDB::Table", {
                TableName: "Transactions-prod",
                KeySchema: [
                    { AttributeName: "yearMonth", KeyType: "HASH" },
                    { AttributeName: "sortId", KeyType: "RANGE" },
                ],
            });
        });

        test("IncomeSource table has correct key schema", () => {
            template.hasResourceProperties("AWS::DynamoDB::Table", {
                TableName: "IncomeSource-prod",
                KeySchema: [{ AttributeName: "incomeSourceId", KeyType: "HASH" }],
            });
        });

        test("PlannedIncome table has correct key schema", () => {
            template.hasResourceProperties("AWS::DynamoDB::Table", {
                TableName: "PlannedIncome-prod",
                KeySchema: [
                    { AttributeName: "yearMonth", KeyType: "HASH" },
                    { AttributeName: "plannedIncomeId", KeyType: "RANGE" },
                ],
            });
        });

        test("Profile table has correct key schema", () => {
            template.hasResourceProperties("AWS::DynamoDB::Table", {
                TableName: "Profile-prod",
                KeySchema: [
                    { AttributeName: "householdId", KeyType: "HASH" },
                    { AttributeName: "personId", KeyType: "RANGE" },
                ],
            });
        });

        test("Account table has correct key schema", () => {
            template.hasResourceProperties("AWS::DynamoDB::Table", {
                TableName: "Account-prod",
                KeySchema: [{ AttributeName: "accountId", KeyType: "HASH" }],
            });
        });

        test("NetWorthSnapshot table has correct key schema", () => {
            template.hasResourceProperties("AWS::DynamoDB::Table", {
                TableName: "NetWorthSnapshot-prod",
                KeySchema: [
                    { AttributeName: "yearMonth", KeyType: "HASH" },
                    { AttributeName: "accountId", KeyType: "RANGE" },
                ],
            });
        });

        test("all tables use on-demand billing", () => {
            template.allResourcesProperties("AWS::DynamoDB::Table", {
                BillingMode: "PAY_PER_REQUEST",
            });
        });

        test("all tables have PITR enabled", () => {
            template.allResourcesProperties("AWS::DynamoDB::Table", {
                PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
            });
        });

        test("prod tables have RETAIN removal policy", () => {
            template.allResources("AWS::DynamoDB::Table", {
                UpdateReplacePolicy: "Retain",
                DeletionPolicy: "Retain",
            });
        });
    });

    describe("dev stage", () => {
        let template: Template;

        beforeAll(() => {
            template = createTestTemplate("dev");
        });

        test("dev tables use stage-suffixed names", () => {
            template.hasResourceProperties("AWS::DynamoDB::Table", {
                TableName: "Category-dev",
            });
            template.hasResourceProperties("AWS::DynamoDB::Table", {
                TableName: "Budget-dev",
            });
        });

        test("dev tables have DELETE removal policy", () => {
            template.allResources("AWS::DynamoDB::Table", {
                UpdateReplacePolicy: "Delete",
                DeletionPolicy: "Delete",
            });
        });
    });
});
