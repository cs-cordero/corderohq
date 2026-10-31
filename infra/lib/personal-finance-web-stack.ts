import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as apigatewayv2 from "aws-cdk-lib/aws-apigatewayv2";
import * as apigatewayv2Authorizers from "aws-cdk-lib/aws-apigatewayv2-authorizers";
import * as apigatewayv2Integrations from "aws-cdk-lib/aws-apigatewayv2-integrations";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as route53Targets from "aws-cdk-lib/aws-route53-targets";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment";
import * as scheduler from "aws-cdk-lib/aws-scheduler";
import type * as sns from "aws-cdk-lib/aws-sns";
import type { Construct } from "constructs";
import { addLambdaErrorAlarm } from "./alarms";
import type { Stage } from "./constants";
import { AUTH_DOMAIN, personalFinanceDomain, personalFinanceOrigin } from "./constants";

interface PersonalFinanceWebStackProps extends cdk.StackProps {
    readonly stage: Stage;
    readonly userPool: cognito.IUserPool;
    readonly rootZone: route53.IHostedZone;
    readonly certificate: acm.ICertificate;
    readonly categoryGroupTable: dynamodb.ITable;
    readonly categoryTable: dynamodb.ITable;
    readonly budgetTable: dynamodb.ITable;
    readonly budgetAuditLogTable: dynamodb.ITable;
    readonly transactionsTable: dynamodb.ITable;
    readonly incomeSourceTable: dynamodb.ITable;
    readonly plannedIncomeTable: dynamodb.ITable;
    readonly profileTable: dynamodb.ITable;
    readonly accountTable: dynamodb.ITable;
    readonly netWorthSnapshotTable: dynamodb.ITable;
    readonly alertTopic: sns.ITopic;
}

/**
 * Personal finance web app — React frontend served via CloudFront, API via API Gateway.
 *
 * AWS resources:
 * - Cognito User Pool Client: personal-finance-web-app-{stage} (authorization code grant, PKCE)
 * - Cognito User Pool Group: budget-admin-{stage} (admin override authorization for budget feature)
 * - CloudWatch log group: /aws/lambda/personal-finance-handler-{stage}
 * - Lambda function: personal-finance-handler-{stage} (Python 3.13)
 * - IAM policy: DynamoDB read/write on all personal-finance tables (incl. IncomeSource, PlannedIncome, Profile, Account), SSM GetParameter for API key
 * - CloudWatch log group: /aws/lambda/budget-scheduled-densify-{stage}
 * - Lambda function: budget-scheduled-densify-{stage} (Python 3.13, cron-driven; densifies Budget and PlannedIncome)
 * - IAM role: scheduler invoke role for the densify Lambda
 * - EventBridge Schedule: budget-scheduled-densify-{stage} (cron(0 0 1 * ? *) America/New_York)
 * - CloudWatch alarm (prod stage only): personal-finance-handler-prod-errors → shared alert topic
 * - CloudWatch alarm (prod stage only): budget-scheduled-densify-prod-errors → shared alert topic
 * - API Gateway HTTP API: personal-finance-api-{stage} (Cognito JWT authorizer)
 * - Lambda permission: one for the whole HTTP API to invoke personal-finance-handler-{stage}
 * - API Gateway routes: /api/category-groups (+ /{groupId} rename / deactivate / reorder), /api/categories (+ /{categoryId} GET-preview / PUT / DELETE / reactivate / deactivate / reorder), /api/budget/{yearMonth} (+ /replace, /update), /api/transactions (+ /upload, /commit, /update, /delete), /api/income-sources (+ /reorder, /{incomeSourceId} PUT / deactivate / reactivate), /api/income/{yearMonth} (+ /replace, /update, /one-time), /api/summary, /api/audit-log, /api/profiles (GET / POST, + /{profileId} PUT / delete), /api/accounts (+ /reorder, /{accountId} PUT / deactivate / reactivate), /api/net-worth/history, /api/net-worth/{yearMonth} (GET / POST)
 * - S3 bucket: personal-finance-assets-{stage}-{account}-{region} (frontend assets)
 * - CloudFront distribution: S3 origin (frontend) + API Gateway origin (/api/*) + Cognito proxy (/oauth2/*)
 * - Route 53 A record: {stage domain} → CloudFront
 * - S3 BucketDeployment: web/personal-finance/dist → assets bucket (CloudFront invalidation, config.json injection)
 */
export class PersonalFinanceWebStack extends cdk.Stack {
    constructor(scope: Construct, id: string, props: PersonalFinanceWebStackProps) {
        super(scope, id, props);

        const { stage } = props;

        cdk.Tags.of(this).add("project", "personal-finance");
        cdk.Tags.of(this).add("stage", stage);

        const userPoolClient = this.createUserPoolClient(props.userPool, stage);
        this.createAdminGroup(props.userPool, stage);
        const apiHandler = this.createLambda(props, stage);
        const densifyFn = this.createScheduledDensifier(props, stage);

        // Alarms for the prod stage only. The dev Lambdas fail often during development, and
        // those failures are expected. An alarm on dev would make noise and hide a real one.
        if (stage === "prod") {
            addLambdaErrorAlarm(this, "ApiHandlerErrorAlarm", apiHandler, props.alertTopic);
            addLambdaErrorAlarm(this, "ScheduledDensifyErrorAlarm", densifyFn, props.alertTopic);
        }

        const httpApi = this.createApiGateway(props, userPoolClient, apiHandler, stage);
        const { distribution, assetsBucket } = this.createCloudFront(httpApi, stage, props.certificate);
        this.createDnsRecords(props.rootZone, distribution, stage);
        this.deployFrontend(assetsBucket, distribution, userPoolClient, stage);

        new cdk.CfnOutput(this, "HttpApiUrl", {
            value: httpApi.apiEndpoint,
            description: `Personal finance HTTP API base URL (${stage}) — used as Vite /api proxy target in dev`,
        });
    }

    private createAdminGroup(userPool: cognito.IUserPool, stage: Stage): void {
        new cognito.CfnUserPoolGroup(this, "BudgetAdminGroup", {
            userPoolId: userPool.userPoolId,
            groupName: `budget-admin-${stage}`,
            description: `Budget admins (${stage}). Members can override locked-month rules and perform hard deletes.`,
        });
    }

    private createUserPoolClient(userPool: cognito.IUserPool, stage: Stage): cognito.UserPoolClient {
        const origin = personalFinanceOrigin(stage);
        const callbackUrls = [`${origin}/callback`];
        const logoutUrls = [`${origin}/`];
        if (stage === "dev") {
            callbackUrls.push("http://localhost:5174/callback");
            logoutUrls.push("http://localhost:5174/");
        }

        return new cognito.UserPoolClient(this, "WebAppClient", {
            userPool,
            userPoolClientName: `personal-finance-web-app-${stage}`,
            generateSecret: false,
            preventUserExistenceErrors: true,
            enableTokenRevocation: true,
            authSessionValidity: cdk.Duration.minutes(3),
            idTokenValidity: cdk.Duration.minutes(15),
            accessTokenValidity: cdk.Duration.minutes(15),
            refreshTokenValidity: cdk.Duration.days(1),
            oAuth: {
                flows: { authorizationCodeGrant: true },
                callbackUrls,
                logoutUrls,
                scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
            },
        });
    }

    private createLambda(props: PersonalFinanceWebStackProps, stage: Stage): lambda.Function {
        const logGroup = new logs.LogGroup(this, "ApiHandlerLogGroup", {
            logGroupName: `/aws/lambda/personal-finance-handler-${stage}`,
            retention: logs.RetentionDays.ONE_MONTH,
            removalPolicy: cdk.RemovalPolicy.DESTROY,
        });

        const apiHandler = new lambda.Function(this, "ApiHandler", {
            functionName: `personal-finance-handler-${stage}`,
            runtime: lambda.Runtime.PYTHON_3_13,
            handler: "corderohq.personal_finance_handler.handler",
            code: lambda.Code.fromAsset("../lambda", {
                bundling: {
                    image: lambda.Runtime.PYTHON_3_13.bundlingImage,
                    user: "root",
                    command: [
                        "bash",
                        "-c",
                        [
                            "pip install -r requirements-personal-finance.txt -t /asset-output",
                            "cp -r src/corderohq /asset-output/",
                        ].join(" && "),
                    ],
                },
            }),
            memorySize: 1024,
            timeout: cdk.Duration.minutes(5),
            logGroup,
            environment: {
                CATEGORY_GROUP_TABLE_NAME: props.categoryGroupTable.tableName,
                CATEGORY_TABLE_NAME: props.categoryTable.tableName,
                BUDGET_TABLE_NAME: props.budgetTable.tableName,
                BUDGET_AUDIT_LOG_TABLE_NAME: props.budgetAuditLogTable.tableName,
                TRANSACTIONS_TABLE_NAME: props.transactionsTable.tableName,
                INCOME_SOURCE_TABLE_NAME: props.incomeSourceTable.tableName,
                PLANNED_INCOME_TABLE_NAME: props.plannedIncomeTable.tableName,
                PROFILE_TABLE_NAME: props.profileTable.tableName,
                ACCOUNT_TABLE_NAME: props.accountTable.tableName,
                NETWORTH_SNAPSHOT_TABLE_NAME: props.netWorthSnapshotTable.tableName,
                SSM_API_KEY_PARAM: `/personal-finance/${stage}/anthropic-api-key`,
                STAGE: stage,
            },
        });

        props.categoryGroupTable.grantReadWriteData(apiHandler);
        props.categoryTable.grantReadWriteData(apiHandler);
        props.budgetTable.grantReadWriteData(apiHandler);
        props.budgetAuditLogTable.grantReadWriteData(apiHandler);
        props.transactionsTable.grantReadWriteData(apiHandler);
        props.incomeSourceTable.grantReadWriteData(apiHandler);
        props.plannedIncomeTable.grantReadWriteData(apiHandler);
        props.profileTable.grantReadWriteData(apiHandler);
        props.accountTable.grantReadWriteData(apiHandler);
        props.netWorthSnapshotTable.grantReadWriteData(apiHandler);

        apiHandler.addToRolePolicy(
            new iam.PolicyStatement({
                actions: ["ssm:GetParameter"],
                resources: [
                    cdk.Arn.format(
                        {
                            service: "ssm",
                            resource: "parameter",
                            resourceName: "personal-finance/*",
                        },
                        this,
                    ),
                ],
            }),
        );

        return apiHandler;
    }

    private createScheduledDensifier(props: PersonalFinanceWebStackProps, stage: Stage): lambda.Function {
        // A Lambda for the cron path only. It uses the same code package as the API handler,
        // built in the same way, but it calls corderohq.scheduled_densify.handler. The densify
        // step touches no transaction and no audit entry, so this Lambda needs fewer IAM
        // permissions.
        const logGroup = new logs.LogGroup(this, "ScheduledDensifyLogGroup", {
            logGroupName: `/aws/lambda/budget-scheduled-densify-${stage}`,
            retention: logs.RetentionDays.ONE_MONTH,
            removalPolicy: cdk.RemovalPolicy.DESTROY,
        });

        const densifyFn = new lambda.Function(this, "ScheduledDensifyHandler", {
            functionName: `budget-scheduled-densify-${stage}`,
            runtime: lambda.Runtime.PYTHON_3_13,
            handler: "corderohq.scheduled_densify.handler",
            code: lambda.Code.fromAsset("../lambda", {
                bundling: {
                    image: lambda.Runtime.PYTHON_3_13.bundlingImage,
                    user: "root",
                    command: [
                        "bash",
                        "-c",
                        [
                            "pip install -r requirements-personal-finance.txt -t /asset-output",
                            "cp -r src/corderohq /asset-output/",
                        ].join(" && "),
                    ],
                },
            }),
            memorySize: 512,
            timeout: cdk.Duration.minutes(5),
            logGroup,
            environment: {
                CATEGORY_TABLE_NAME: props.categoryTable.tableName,
                BUDGET_TABLE_NAME: props.budgetTable.tableName,
                PLANNED_INCOME_TABLE_NAME: props.plannedIncomeTable.tableName,
                STAGE: stage,
            },
        });

        props.categoryTable.grantReadData(densifyFn);
        props.budgetTable.grantReadWriteData(densifyFn);
        props.plannedIncomeTable.grantReadWriteData(densifyFn);

        // EventBridge Scheduler needs a role of its own to call the Lambda.
        const schedulerRole = new iam.Role(this, "ScheduledDensifySchedulerRole", {
            assumedBy: new iam.ServicePrincipal("scheduler.amazonaws.com"),
        });
        densifyFn.grantInvoke(schedulerRole);

        // Midnight ET on day 1 of each month. ScheduleExpressionTimezone keeps the time
        // correct through a daylight saving change. The architecture spec needs ET.
        new scheduler.CfnSchedule(this, "ScheduledDensifySchedule", {
            name: `budget-scheduled-densify-${stage}`,
            scheduleExpression: "cron(0 0 1 * ? *)",
            scheduleExpressionTimezone: "America/New_York",
            flexibleTimeWindow: { mode: "OFF" },
            target: {
                arn: densifyFn.functionArn,
                roleArn: schedulerRole.roleArn,
            },
            description: `Monthly budget densification trigger (${stage})`,
        });

        return densifyFn;
    }

    private createApiGateway(
        props: PersonalFinanceWebStackProps,
        userPoolClient: cognito.UserPoolClient,
        apiHandler: lambda.Function,
        stage: Stage,
    ): apigatewayv2.HttpApi {
        const authorizer = new apigatewayv2Authorizers.HttpUserPoolAuthorizer("CognitoAuthorizer", props.userPool, {
            userPoolClients: [userPoolClient],
        });

        // One Lambda permission for the whole API, not one for each route. The Lambda resource policy has a limit
        // of 20,480 bytes, and one statement for each route went over it at 47 routes.
        const integration = new apigatewayv2Integrations.HttpLambdaIntegration("ApiHandlerIntegration", apiHandler, {
            scopePermissionToRoute: false,
        });

        const httpApi = new apigatewayv2.HttpApi(this, "HttpApi", {
            apiName: `personal-finance-api-${stage}`,
            description: `Personal finance API (${stage})`,
            defaultAuthorizer: authorizer,
        });

        // Each path is listed in full. If a path is absent here, API Gateway returns 404, and the
        // Lambda never sees the request. Keep this list the same as the routes in
        // personal_finance_handler.handler.
        const routes: { path: string; methods: apigatewayv2.HttpMethod[] }[] = [
            // Category groups (CP12)
            { path: "/api/category-groups", methods: [apigatewayv2.HttpMethod.GET, apigatewayv2.HttpMethod.POST] },
            {
                path: "/api/category-groups/{groupId}",
                methods: [apigatewayv2.HttpMethod.PUT, apigatewayv2.HttpMethod.DELETE],
            },
            { path: "/api/category-groups/reorder", methods: [apigatewayv2.HttpMethod.POST] },

            // Categories
            { path: "/api/categories", methods: [apigatewayv2.HttpMethod.GET, apigatewayv2.HttpMethod.POST] },
            { path: "/api/categories/reorder", methods: [apigatewayv2.HttpMethod.POST] },
            {
                path: "/api/categories/{categoryId}",
                methods: [
                    apigatewayv2.HttpMethod.GET, // CP9 deletion preview (?deletion_preview=true)
                    apigatewayv2.HttpMethod.PUT,
                    apigatewayv2.HttpMethod.DELETE, // CP9 hard delete
                ],
            },
            { path: "/api/categories/{categoryId}/reactivate", methods: [apigatewayv2.HttpMethod.POST] },
            { path: "/api/categories/{categoryId}/deactivate", methods: [apigatewayv2.HttpMethod.POST] }, // CP8

            // Budget
            { path: "/api/budget/{yearMonth}", methods: [apigatewayv2.HttpMethod.GET] },
            { path: "/api/budget/{yearMonth}/replace", methods: [apigatewayv2.HttpMethod.POST] }, // CP4
            { path: "/api/budget/{yearMonth}/update", methods: [apigatewayv2.HttpMethod.POST] },

            // Transactions
            { path: "/api/transactions", methods: [apigatewayv2.HttpMethod.GET, apigatewayv2.HttpMethod.POST] },
            { path: "/api/transactions/upload", methods: [apigatewayv2.HttpMethod.POST] },
            { path: "/api/transactions/commit", methods: [apigatewayv2.HttpMethod.POST] }, // CP10
            { path: "/api/transactions/update", methods: [apigatewayv2.HttpMethod.POST] },
            { path: "/api/transactions/delete", methods: [apigatewayv2.HttpMethod.POST] },

            // Income sources and the income plan
            { path: "/api/income-sources", methods: [apigatewayv2.HttpMethod.GET, apigatewayv2.HttpMethod.POST] },
            { path: "/api/income-sources/reorder", methods: [apigatewayv2.HttpMethod.POST] },
            { path: "/api/income-sources/{incomeSourceId}", methods: [apigatewayv2.HttpMethod.PUT] },
            { path: "/api/income-sources/{incomeSourceId}/deactivate", methods: [apigatewayv2.HttpMethod.POST] },
            { path: "/api/income-sources/{incomeSourceId}/reactivate", methods: [apigatewayv2.HttpMethod.POST] },
            { path: "/api/income/{yearMonth}", methods: [apigatewayv2.HttpMethod.GET] },
            { path: "/api/income/{yearMonth}/replace", methods: [apigatewayv2.HttpMethod.POST] },
            { path: "/api/income/{yearMonth}/update", methods: [apigatewayv2.HttpMethod.POST] },
            { path: "/api/income/{yearMonth}/one-time", methods: [apigatewayv2.HttpMethod.POST] },

            // Read endpoints
            { path: "/api/summary", methods: [apigatewayv2.HttpMethod.GET] },
            { path: "/api/audit-log", methods: [apigatewayv2.HttpMethod.GET] },

            // Household profile (net worth)
            { path: "/api/profiles", methods: [apigatewayv2.HttpMethod.GET, apigatewayv2.HttpMethod.POST] },
            { path: "/api/profiles/{profileId}", methods: [apigatewayv2.HttpMethod.PUT] },
            { path: "/api/profiles/{profileId}/delete", methods: [apigatewayv2.HttpMethod.POST] },

            // Accounts (net worth)
            { path: "/api/accounts", methods: [apigatewayv2.HttpMethod.GET, apigatewayv2.HttpMethod.POST] },
            { path: "/api/accounts/reorder", methods: [apigatewayv2.HttpMethod.POST] },
            { path: "/api/accounts/{accountId}", methods: [apigatewayv2.HttpMethod.PUT] },
            { path: "/api/accounts/{accountId}/deactivate", methods: [apigatewayv2.HttpMethod.POST] },
            { path: "/api/accounts/{accountId}/reactivate", methods: [apigatewayv2.HttpMethod.POST] },

            // The net worth snapshots. The fixed /history route sits beside the /{yearMonth}
            // route. An HTTP API matches the exact segment first.
            { path: "/api/net-worth/history", methods: [apigatewayv2.HttpMethod.GET] },
            {
                path: "/api/net-worth/{yearMonth}",
                methods: [apigatewayv2.HttpMethod.GET, apigatewayv2.HttpMethod.POST],
            },
        ];

        for (const route of routes) {
            httpApi.addRoutes({
                path: route.path,
                methods: route.methods,
                integration,
            });
        }

        return httpApi;
    }

    private createCloudFront(
        httpApi: apigatewayv2.HttpApi,
        stage: Stage,
        certificate: acm.ICertificate,
    ): { distribution: cloudfront.Distribution; assetsBucket: s3.Bucket } {
        const domain = personalFinanceDomain(stage);

        // This bucket holds the React build and config.json, and nothing else. A
        // `npm run build` and a deploy make all of it again, so DESTROY is correct in both
        // stages. A stack delete then needs no special care.
        const assetsBucket = new s3.Bucket(this, "AssetsBucket", {
            bucketName: `personal-finance-assets-${stage}-${this.account}-${this.region}`,
            blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
            encryption: s3.BucketEncryption.S3_MANAGED,
            removalPolicy: cdk.RemovalPolicy.DESTROY,
            autoDeleteObjects: true,
        });

        // The origin request policy for API Gateway. It forwards every viewer header, and that
        // includes Authorization.
        const apiOriginRequestPolicy = cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER;

        const apiOriginDomain = `${httpApi.httpApiId}.execute-api.${this.region}.amazonaws.com`;

        const distribution = new cloudfront.Distribution(this, "Distribution", {
            comment: `Personal finance app (${stage})`,
            defaultBehavior: {
                origin: origins.S3BucketOrigin.withOriginAccessControl(assetsBucket),
                viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cachePolicy: stage === "dev" ? cloudfront.CachePolicy.CACHING_DISABLED : undefined,
            },
            additionalBehaviors: {
                "/api/*": {
                    origin: new origins.HttpOrigin(apiOriginDomain),
                    viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
                    originRequestPolicy: apiOriginRequestPolicy,
                    allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
                },
                "/oauth2/*": {
                    origin: new origins.HttpOrigin(AUTH_DOMAIN),
                    viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
                    originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                    allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
                },
            },
            defaultRootObject: "index.html",
            errorResponses: [
                {
                    httpStatus: 403,
                    responseHttpStatus: 200,
                    responsePagePath: "/index.html",
                },
                {
                    httpStatus: 404,
                    responseHttpStatus: 200,
                    responsePagePath: "/index.html",
                },
            ],
            priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
            domainNames: [domain],
            certificate,
        });

        return { distribution, assetsBucket };
    }

    private deployFrontend(
        assetsBucket: s3.Bucket,
        distribution: cloudfront.Distribution,
        userPoolClient: cognito.UserPoolClient,
        stage: Stage,
    ): void {
        new s3deploy.BucketDeployment(this, "FrontendDeployment", {
            sources: [
                s3deploy.Source.asset("../web/personal-finance/dist"),
                s3deploy.Source.jsonData("config.json", {
                    cognitoDomain: "https://auth.corderohq.com",
                    clientId: userPoolClient.userPoolClientId,
                    stage,
                }),
            ],
            destinationBucket: assetsBucket,
            ...(stage === "prod" ? { distribution, distributionPaths: ["/*"] } : {}),
        });

        new cdk.CfnOutput(this, "UserPoolClientId", {
            value: userPoolClient.userPoolClientId,
            description: `Personal finance app Cognito client ID (${stage})`,
        });
    }

    private createDnsRecords(rootZone: route53.IHostedZone, distribution: cloudfront.Distribution, stage: Stage): void {
        const domain = personalFinanceDomain(stage);

        new route53.ARecord(this, "CloudFrontAlias", {
            zone: rootZone,
            recordName: domain,
            target: route53.RecordTarget.fromAlias(new route53Targets.CloudFrontTarget(distribution)),
        });
    }
}
