import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as sns from "aws-cdk-lib/aws-sns";
import { Match, Template } from "aws-cdk-lib/assertions";
import { PersonalFinanceWebStack } from "../lib/personal-finance-web-stack";

function createTestTemplate(stage: "dev" | "prod"): Template {
    const app = new cdk.App();
    const env = { account: "123456789012", region: "us-east-1" };

    const helperStack = new cdk.Stack(app, "HelperStack", { env });
    const userPool = new cognito.UserPool(helperStack, "UserPool");
    const rootZone = new route53.HostedZone(helperStack, "RootZone", {
        zoneName: "corderohq.com",
    });
    const certificate = new acm.Certificate(helperStack, "SharedCert", {
        domainName: "corderohq.com",
    });
    const categoryGroupTable = new dynamodb.Table(helperStack, "CategoryGroupTable", {
        partitionKey: { name: "groupId", type: dynamodb.AttributeType.STRING },
    });
    const categoryTable = new dynamodb.Table(helperStack, "CategoryTable", {
        partitionKey: { name: "categoryId", type: dynamodb.AttributeType.STRING },
    });
    const budgetTable = new dynamodb.Table(helperStack, "BudgetTable", {
        partitionKey: { name: "yearMonth", type: dynamodb.AttributeType.STRING },
        sortKey: { name: "categoryId", type: dynamodb.AttributeType.STRING },
    });
    const budgetAuditLogTable = new dynamodb.Table(helperStack, "BudgetAuditLogTable", {
        partitionKey: { name: "entityType", type: dynamodb.AttributeType.STRING },
        sortKey: { name: "sortId", type: dynamodb.AttributeType.STRING },
    });
    const transactionsTable = new dynamodb.Table(helperStack, "TransactionsTable", {
        partitionKey: { name: "yearMonth", type: dynamodb.AttributeType.STRING },
        sortKey: { name: "sortId", type: dynamodb.AttributeType.STRING },
    });
    const incomeSourceTable = new dynamodb.Table(helperStack, "IncomeSourceTable", {
        partitionKey: { name: "incomeSourceId", type: dynamodb.AttributeType.STRING },
    });
    const plannedIncomeTable = new dynamodb.Table(helperStack, "PlannedIncomeTable", {
        partitionKey: { name: "yearMonth", type: dynamodb.AttributeType.STRING },
        sortKey: { name: "plannedIncomeId", type: dynamodb.AttributeType.STRING },
    });
    const profileTable = new dynamodb.Table(helperStack, "ProfileTable", {
        partitionKey: { name: "householdId", type: dynamodb.AttributeType.STRING },
        sortKey: { name: "personId", type: dynamodb.AttributeType.STRING },
    });
    const accountTable = new dynamodb.Table(helperStack, "AccountTable", {
        partitionKey: { name: "accountId", type: dynamodb.AttributeType.STRING },
    });
    const netWorthSnapshotTable = new dynamodb.Table(helperStack, "NetWorthSnapshotTable", {
        partitionKey: { name: "yearMonth", type: dynamodb.AttributeType.STRING },
        sortKey: { name: "accountId", type: dynamodb.AttributeType.STRING },
    });

    const alertTopic = new sns.Topic(helperStack, "AlertTopic");

    const stack = new PersonalFinanceWebStack(app, "TestPersonalFinanceWebStack", {
        env,
        stage,
        userPool,
        rootZone,
        certificate,
        alertTopic,
        categoryGroupTable,
        categoryTable,
        budgetTable,
        budgetAuditLogTable,
        transactionsTable,
        incomeSourceTable,
        plannedIncomeTable,
        profileTable,
        accountTable,
        netWorthSnapshotTable,
    });

    return Template.fromStack(stack);
}

describe("PersonalFinanceWebStack", () => {
    describe("prod stage", () => {
        let template: Template;

        beforeAll(() => {
            template = createTestTemplate("prod");
        });

        test("creates a Cognito User Pool Client for the personal finance app", () => {
            template.hasResourceProperties("AWS::Cognito::UserPoolClient", {
                ClientName: "personal-finance-web-app-prod",
                AllowedOAuthFlows: ["code"],
            });
        });

        test("callback URL uses prod personal finance domain", () => {
            template.hasResourceProperties("AWS::Cognito::UserPoolClient", {
                CallbackURLs: ["https://finance.corderohq.com/callback"],
            });
        });

        test("creates a Lambda function", () => {
            template.hasResourceProperties("AWS::Lambda::Function", {
                FunctionName: "personal-finance-handler-prod",
                Runtime: "python3.13",
                Handler: "corderohq.personal_finance_handler.handler",
            });
        });

        test("Lambda has DynamoDB table name environment variables", () => {
            template.hasResourceProperties("AWS::Lambda::Function", {
                Environment: {
                    Variables: Match.objectLike({
                        CATEGORY_TABLE_NAME: Match.anyValue(),
                        BUDGET_TABLE_NAME: Match.anyValue(),
                        BUDGET_AUDIT_LOG_TABLE_NAME: Match.anyValue(),
                        TRANSACTIONS_TABLE_NAME: Match.anyValue(),
                        INCOME_SOURCE_TABLE_NAME: Match.anyValue(),
                        PLANNED_INCOME_TABLE_NAME: Match.anyValue(),
                        PROFILE_TABLE_NAME: Match.anyValue(),
                        ACCOUNT_TABLE_NAME: Match.anyValue(),
                        NETWORTH_SNAPSHOT_TABLE_NAME: Match.anyValue(),
                        SSM_API_KEY_PARAM: "/personal-finance/prod/anthropic-api-key",
                    }),
                },
            });
        });

        test("scheduled densify Lambda has the budget and planned income table names", () => {
            template.hasResourceProperties("AWS::Lambda::Function", {
                FunctionName: "budget-scheduled-densify-prod",
                Environment: {
                    Variables: Match.objectLike({
                        CATEGORY_TABLE_NAME: Match.anyValue(),
                        BUDGET_TABLE_NAME: Match.anyValue(),
                        PLANNED_INCOME_TABLE_NAME: Match.anyValue(),
                    }),
                },
            });
        });

        test("API Gateway has the income routes", () => {
            for (const routeKey of [
                "GET /api/income-sources",
                "POST /api/income-sources",
                "POST /api/income-sources/reorder",
                "PUT /api/income-sources/{incomeSourceId}",
                "POST /api/income-sources/{incomeSourceId}/deactivate",
                "POST /api/income-sources/{incomeSourceId}/reactivate",
                "GET /api/income/{yearMonth}",
                "POST /api/income/{yearMonth}/replace",
                "POST /api/income/{yearMonth}/pin",
                "POST /api/income/{yearMonth}/one-time",
            ]) {
                template.hasResourceProperties("AWS::ApiGatewayV2::Route", { RouteKey: routeKey });
            }
        });

        test("creates an API Gateway HTTP API", () => {
            template.hasResourceProperties("AWS::ApiGatewayV2::Api", {
                Name: "personal-finance-api-prod",
                ProtocolType: "HTTP",
            });
        });

        test("creates a CloudFront distribution with prod domain", () => {
            template.hasResourceProperties("AWS::CloudFront::Distribution", {
                DistributionConfig: Match.objectLike({
                    Aliases: ["finance.corderohq.com"],
                }),
            });
        });

        test("CloudFront has error responses for SPA routing", () => {
            template.hasResourceProperties("AWS::CloudFront::Distribution", {
                DistributionConfig: Match.objectLike({
                    CustomErrorResponses: Match.arrayWith([
                        Match.objectLike({
                            ErrorCode: 404,
                            ResponseCode: 200,
                            ResponsePagePath: "/index.html",
                        }),
                    ]),
                }),
            });
        });

        test("creates a Route 53 A record for the personal finance domain", () => {
            template.hasResourceProperties("AWS::Route53::RecordSet", {
                Name: "finance.corderohq.com.",
                Type: "A",
            });
        });

        test("Lambda has SSM GetParameter permission", () => {
            template.hasResourceProperties("AWS::IAM::Policy", {
                PolicyDocument: {
                    Statement: Match.arrayWith([
                        Match.objectLike({
                            Action: "ssm:GetParameter",
                            Resource: Match.objectLike({
                                "Fn::Join": Match.anyValue(),
                            }),
                        }),
                    ]),
                },
            });
        });
    });

    describe("dev stage", () => {
        let template: Template;

        beforeAll(() => {
            template = createTestTemplate("dev");
        });

        test("dev Lambda uses dev function name", () => {
            template.hasResourceProperties("AWS::Lambda::Function", {
                FunctionName: "personal-finance-handler-dev",
            });
        });

        test("dev API Gateway uses dev name", () => {
            template.hasResourceProperties("AWS::ApiGatewayV2::Api", {
                Name: "personal-finance-api-dev",
            });
        });

        test("dev CloudFront uses dev domain", () => {
            template.hasResourceProperties("AWS::CloudFront::Distribution", {
                DistributionConfig: Match.objectLike({
                    Aliases: ["dev.finance.corderohq.com"],
                }),
            });
        });

        test("dev callback URL uses dev personal finance domain", () => {
            template.hasResourceProperties("AWS::Cognito::UserPoolClient", {
                CallbackURLs: Match.arrayWith(["https://dev.finance.corderohq.com/callback"]),
            });
        });

        test("dev Route 53 A record uses dev domain", () => {
            template.hasResourceProperties("AWS::Route53::RecordSet", {
                Name: "dev.finance.corderohq.com.",
                Type: "A",
            });
        });
    });
});
