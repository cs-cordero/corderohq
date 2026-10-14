import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as apigatewayv2 from "aws-cdk-lib/aws-apigatewayv2";
import * as apigatewayv2Authorizers from "aws-cdk-lib/aws-apigatewayv2-authorizers";
import * as apigatewayv2Integrations from "aws-cdk-lib/aws-apigatewayv2-integrations";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as route53Targets from "aws-cdk-lib/aws-route53-targets";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment";
import type * as sns from "aws-cdk-lib/aws-sns";
import type { Construct } from "constructs";
import { addLambdaErrorAlarm } from "./alarms";
import { API_DOMAIN, HSA_ORIGIN } from "./constants";

interface HsaWebStackProps extends cdk.StackProps {
    readonly userPool: cognito.IUserPool;
    readonly assetsBucket: s3.IBucket;
    readonly distribution: cloudfront.IDistribution;
    readonly dataBucket: s3.IBucket;
    readonly processorFunction: lambda.IFunction;
    readonly rootZone: route53.IHostedZone;
    readonly certificate: acm.ICertificate;
    readonly alertTopic: sns.ITopic;
}

/**
 * HSA web UI — ledger editor and receipt upload.
 *
 * AWS resources:
 * - Cognito User Pool Client: hsa-web-app (authorization code grant, PKCE)
 * - CloudWatch log group: /aws/lambda/hsa-web-handler
 * - Lambda function: hsa-web-handler (Python 3.13, boto3 only)
 * - CloudWatch alarm: hsa-web-handler-errors → shared alert topic
 * - IAM policy: S3 read/write on data bucket, Lambda invoke on processor
 * - API Gateway HTTP API: hsa-web-api (Cognito JWT authorizer, CORS)
 * - API Gateway routes: GET /ledger, PUT /ledger, GET /receipt, POST /receipt, DELETE /receipt, GET /orphaned-receipts
 * - API Gateway custom domain: api.hsa.corderohq.com
 * - API Gateway API mapping: api.hsa.corderohq.com → hsa-web-api
 * - Route 53 A record: api.hsa.corderohq.com → API Gateway
 * - S3 BucketDeployment: web/hsa/ → assets bucket hsa/ prefix (CloudFront invalidation)
 */
export class HsaWebStack extends cdk.Stack {
    constructor(scope: Construct, id: string, props: HsaWebStackProps) {
        super(scope, id, props);

        cdk.Tags.of(this).add("project", "hsa-web");

        const userPoolClient = this.createUserPoolClient(props.userPool);
        const webHandler = this.createLambda(props.dataBucket, props.processorFunction);
        addLambdaErrorAlarm(this, "WebHandlerErrorAlarm", webHandler, props.alertTopic);
        this.createApiGateway(props, userPoolClient, webHandler);

        // Deploy static web files to the shared assets bucket. `followSymlinks: EXTERNAL`
        // resolves the favicon.svg symlink, which points outside web/hsa/ to web/shared/ so
        // that every frontend shares one icon.
        new s3deploy.BucketDeployment(this, "WebAssets", {
            sources: [
                s3deploy.Source.asset("../web/hsa", {
                    followSymlinks: cdk.SymlinkFollowMode.EXTERNAL,
                }),
            ],
            destinationBucket: props.assetsBucket,
            destinationKeyPrefix: "hsa",
            distribution: props.distribution,
            distributionPaths: ["/*"],
        });
    }

    private createUserPoolClient(userPool: cognito.IUserPool): cognito.UserPoolClient {
        return new cognito.UserPoolClient(this, "WebAppClient", {
            userPool,
            userPoolClientName: "hsa-web-app",
            generateSecret: false,
            preventUserExistenceErrors: true,
            enableTokenRevocation: true,
            authSessionValidity: cdk.Duration.minutes(3),
            idTokenValidity: cdk.Duration.minutes(15),
            accessTokenValidity: cdk.Duration.minutes(15),
            refreshTokenValidity: cdk.Duration.days(1),
            oAuth: {
                flows: { authorizationCodeGrant: true },
                callbackUrls: [`${HSA_ORIGIN}/callback.html`],
                logoutUrls: [`${HSA_ORIGIN}/`],
                scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
            },
        });
    }

    private createLambda(dataBucket: s3.IBucket, processorFunction: lambda.IFunction): lambda.Function {
        const logGroup = new logs.LogGroup(this, "WebHandlerLogGroup", {
            logGroupName: "/aws/lambda/hsa-web-handler",
            retention: logs.RetentionDays.ONE_MONTH,
            removalPolicy: cdk.RemovalPolicy.DESTROY,
        });

        const webHandler = new lambda.Function(this, "WebHandler", {
            functionName: "hsa-web-handler",
            runtime: lambda.Runtime.PYTHON_3_13,
            handler: "corderohq.web_handler.handle",
            code: lambda.Code.fromAsset("../lambda", {
                bundling: {
                    image: lambda.Runtime.PYTHON_3_13.bundlingImage,
                    user: "root",
                    command: [
                        "bash",
                        "-c",
                        [
                            "pip install -r requirements-web.txt -t /asset-output",
                            "cp -r src/corderohq /asset-output/",
                        ].join(" && "),
                    ],
                },
            }),
            memorySize: 1024,
            timeout: cdk.Duration.minutes(5),
            logGroup,
            environment: {
                BUCKET_NAME: dataBucket.bucketName,
                PROCESSOR_FUNCTION_NAME: processorFunction.functionName,
            },
        });

        dataBucket.grantReadWrite(webHandler);
        processorFunction.grantInvoke(webHandler);

        return webHandler;
    }

    private createApiGateway(
        props: HsaWebStackProps,
        userPoolClient: cognito.UserPoolClient,
        webHandler: lambda.Function,
    ): void {
        const authorizer = new apigatewayv2Authorizers.HttpUserPoolAuthorizer("CognitoAuthorizer", props.userPool, {
            userPoolClients: [userPoolClient],
        });

        const httpIntegration = new apigatewayv2Integrations.HttpLambdaIntegration("WebHandlerIntegration", webHandler);

        const httpApi = new apigatewayv2.HttpApi(this, "HttpApi", {
            apiName: "hsa-web-api",
            description: "HSA Receipts Web API",
            defaultAuthorizer: authorizer,
            corsPreflight: {
                allowOrigins: [HSA_ORIGIN],
                allowMethods: [
                    apigatewayv2.CorsHttpMethod.GET,
                    apigatewayv2.CorsHttpMethod.PUT,
                    apigatewayv2.CorsHttpMethod.POST,
                    apigatewayv2.CorsHttpMethod.DELETE,
                    apigatewayv2.CorsHttpMethod.OPTIONS,
                ],
                allowHeaders: ["Authorization", "Content-Type"],
                maxAge: cdk.Duration.hours(1),
            },
        });

        httpApi.addRoutes({
            path: "/ledger",
            methods: [apigatewayv2.HttpMethod.GET],
            integration: httpIntegration,
        });

        httpApi.addRoutes({
            path: "/ledger",
            methods: [apigatewayv2.HttpMethod.PUT],
            integration: httpIntegration,
        });

        httpApi.addRoutes({
            path: "/receipt",
            methods: [apigatewayv2.HttpMethod.GET],
            integration: httpIntegration,
        });

        httpApi.addRoutes({
            path: "/receipt",
            methods: [apigatewayv2.HttpMethod.POST],
            integration: httpIntegration,
        });

        httpApi.addRoutes({
            path: "/receipt",
            methods: [apigatewayv2.HttpMethod.DELETE],
            integration: httpIntegration,
        });

        httpApi.addRoutes({
            path: "/orphaned-receipts",
            methods: [apigatewayv2.HttpMethod.GET],
            integration: httpIntegration,
        });

        const apiDomain = new apigatewayv2.DomainName(this, "ApiDomainName", {
            domainName: API_DOMAIN,
            certificate: props.certificate,
        });

        new apigatewayv2.ApiMapping(this, "ApiMapping", {
            api: httpApi,
            domainName: apiDomain,
        });

        this.createDnsRecords(props.rootZone, apiDomain);
    }

    private createDnsRecords(rootZone: route53.IHostedZone, apiDomain: apigatewayv2.DomainName): void {
        new route53.ARecord(this, "ApiDnsRecord", {
            zone: rootZone,
            recordName: API_DOMAIN,
            target: route53.RecordTarget.fromAlias(
                new route53Targets.ApiGatewayv2DomainProperties(
                    apiDomain.regionalDomainName,
                    apiDomain.regionalHostedZoneId,
                ),
            ),
        });
    }
}
