import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as sns from "aws-cdk-lib/aws-sns";
import { Match, Template } from "aws-cdk-lib/assertions";
import { HsaWebStack } from "../lib/hsa-web-stack";

function createTestTemplate(): Template {
    const app = new cdk.App();
    const env = { account: "123456789012", region: "us-east-1" };

    const helperStack = new cdk.Stack(app, "HelperStack", { env });
    const userPool = new cognito.UserPool(helperStack, "UserPool");
    const bucket = new s3.Bucket(helperStack, "Bucket");
    const distribution = new cloudfront.Distribution(helperStack, "Distribution", {
        defaultBehavior: {
            origin: origins.S3BucketOrigin.withOriginAccessControl(bucket),
        },
    });
    const fn = new lambda.Function(helperStack, "Fn", {
        runtime: lambda.Runtime.PYTHON_3_13,
        handler: "index.handler",
        code: lambda.Code.fromInline("pass"),
    });
    const rootZone = new route53.HostedZone(helperStack, "RootZone", {
        zoneName: "corderohq.com",
    });
    const certificate = new acm.Certificate(helperStack, "SharedCert", {
        domainName: "corderohq.com",
    });
    const alertTopic = new sns.Topic(helperStack, "AlertTopic");

    const stack = new HsaWebStack(app, "TestHsaWebStack", {
        env,
        userPool,
        assetsBucket: bucket,
        distribution,
        dataBucket: bucket,
        processorFunction: fn,
        rootZone,
        certificate,
        alertTopic,
    });

    return Template.fromStack(stack);
}

describe("HsaWebStack", () => {
    let template: Template;

    beforeAll(() => {
        template = createTestTemplate();
    });

    describe("User Pool Client — OAuth config", () => {
        test("uses authorization code grant only", () => {
            template.hasResourceProperties("AWS::Cognito::UserPoolClient", {
                AllowedOAuthFlows: ["code"],
            });
        });

        test("does not generate a client secret", () => {
            template.hasResourceProperties("AWS::Cognito::UserPoolClient", {
                GenerateSecret: false,
            });
        });

        test("scopes are openid and email only", () => {
            template.hasResourceProperties("AWS::Cognito::UserPoolClient", {
                AllowedOAuthScopes: ["openid", "email"],
            });
        });

        test("only supports COGNITO identity provider", () => {
            template.hasResourceProperties("AWS::Cognito::UserPoolClient", {
                SupportedIdentityProviders: ["COGNITO"],
            });
        });

        test("callback URL points to hsa.corderohq.com/callback.html", () => {
            template.hasResourceProperties("AWS::Cognito::UserPoolClient", {
                CallbackURLs: ["https://hsa.corderohq.com/callback.html"],
            });
        });
    });

    describe("User Pool Client — token lifetimes", () => {
        test("ID token validity is 15 minutes", () => {
            template.hasResourceProperties("AWS::Cognito::UserPoolClient", {
                IdTokenValidity: 15,
                TokenValidityUnits: Match.objectLike({ IdToken: "minutes" }),
            });
        });

        test("access token validity is 15 minutes", () => {
            template.hasResourceProperties("AWS::Cognito::UserPoolClient", {
                AccessTokenValidity: 15,
                TokenValidityUnits: Match.objectLike({ AccessToken: "minutes" }),
            });
        });

        test("refresh token validity is 1 day", () => {
            template.hasResourceProperties("AWS::Cognito::UserPoolClient", {
                RefreshTokenValidity: 1440,
                TokenValidityUnits: Match.objectLike({ RefreshToken: "minutes" }),
            });
        });

        test("auth session validity is 3 minutes", () => {
            template.hasResourceProperties("AWS::Cognito::UserPoolClient", {
                AuthSessionValidity: 3,
            });
        });
    });

    describe("User Pool Client — security", () => {
        test("prevents user existence errors", () => {
            template.hasResourceProperties("AWS::Cognito::UserPoolClient", {
                PreventUserExistenceErrors: "ENABLED",
            });
        });

        test("token revocation is enabled", () => {
            template.hasResourceProperties("AWS::Cognito::UserPoolClient", {
                EnableTokenRevocation: true,
            });
        });
    });

    describe("API Gateway — authorizer", () => {
        test("authorizer type is JWT", () => {
            template.hasResourceProperties("AWS::ApiGatewayV2::Authorizer", {
                AuthorizerType: "JWT",
            });
        });

        test("identity source is Authorization header", () => {
            template.hasResourceProperties("AWS::ApiGatewayV2::Authorizer", {
                IdentitySource: ["$request.header.Authorization"],
            });
        });

        test("JWT issuer is Cognito", () => {
            template.hasResourceProperties("AWS::ApiGatewayV2::Authorizer", {
                JwtConfiguration: Match.objectLike({
                    Issuer: Match.objectLike({
                        "Fn::Join": Match.arrayWith([Match.arrayWith([Match.stringLikeRegexp("cognito-idp")])]),
                    }),
                }),
            });
        });
    });

    describe("API Gateway — routes require authorization", () => {
        test("exactly 6 routes exist", () => {
            template.resourceCountIs("AWS::ApiGatewayV2::Route", 6);
        });

        test("all routes require JWT authorization", () => {
            template.allResourcesProperties("AWS::ApiGatewayV2::Route", {
                AuthorizationType: "JWT",
                AuthorizerId: Match.anyValue(),
                RouteKey: Match.anyValue(),
                ApiId: Match.anyValue(),
                Target: Match.anyValue(),
            });
        });

        test("GET /ledger route exists", () => {
            template.hasResourceProperties("AWS::ApiGatewayV2::Route", {
                RouteKey: "GET /ledger",
                AuthorizationType: "JWT",
            });
        });

        test("PUT /ledger route exists", () => {
            template.hasResourceProperties("AWS::ApiGatewayV2::Route", {
                RouteKey: "PUT /ledger",
                AuthorizationType: "JWT",
            });
        });

        test("GET /receipt route exists", () => {
            template.hasResourceProperties("AWS::ApiGatewayV2::Route", {
                RouteKey: "GET /receipt",
                AuthorizationType: "JWT",
            });
        });

        test("POST /receipt route exists", () => {
            template.hasResourceProperties("AWS::ApiGatewayV2::Route", {
                RouteKey: "POST /receipt",
                AuthorizationType: "JWT",
            });
        });

        test("DELETE /receipt route exists", () => {
            template.hasResourceProperties("AWS::ApiGatewayV2::Route", {
                RouteKey: "DELETE /receipt",
                AuthorizationType: "JWT",
            });
        });

        test("GET /orphaned-receipts route exists", () => {
            template.hasResourceProperties("AWS::ApiGatewayV2::Route", {
                RouteKey: "GET /orphaned-receipts",
                AuthorizationType: "JWT",
            });
        });
    });

    describe("API Gateway — CORS", () => {
        test("allows only GET, PUT, POST, DELETE, OPTIONS methods", () => {
            template.hasResourceProperties("AWS::ApiGatewayV2::Api", {
                CorsConfiguration: {
                    AllowMethods: ["GET", "PUT", "POST", "DELETE", "OPTIONS"],
                    AllowHeaders: Match.anyValue(),
                    AllowOrigins: Match.anyValue(),
                    MaxAge: Match.anyValue(),
                },
            });
        });

        test("allows only Authorization and Content-Type headers", () => {
            template.hasResourceProperties("AWS::ApiGatewayV2::Api", {
                CorsConfiguration: Match.objectLike({
                    AllowHeaders: ["Authorization", "Content-Type"],
                }),
            });
        });

        test("max age is 1 hour", () => {
            template.hasResourceProperties("AWS::ApiGatewayV2::Api", {
                CorsConfiguration: Match.objectLike({
                    MaxAge: 3600,
                }),
            });
        });

        test("allows only hsa.corderohq.com origin", () => {
            template.hasResourceProperties("AWS::ApiGatewayV2::Api", {
                CorsConfiguration: Match.objectLike({
                    AllowOrigins: ["https://hsa.corderohq.com"],
                }),
            });
        });
    });

    describe("API Gateway — custom domain", () => {
        test("api.hsa.corderohq.com domain exists", () => {
            template.hasResourceProperties("AWS::ApiGatewayV2::DomainName", {
                DomainName: "api.hsa.corderohq.com",
            });
        });

        test("API mapping exists", () => {
            template.resourceCountIs("AWS::ApiGatewayV2::ApiMapping", 1);
        });

        test("Route 53 A record for api.hsa.corderohq.com", () => {
            template.hasResourceProperties("AWS::Route53::RecordSet", {
                Name: "api.hsa.corderohq.com.",
                Type: "A",
            });
        });
    });
});
