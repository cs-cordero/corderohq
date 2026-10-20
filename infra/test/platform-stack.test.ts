import * as cdk from "aws-cdk-lib";
import * as route53 from "aws-cdk-lib/aws-route53";
import { Match, Template } from "aws-cdk-lib/assertions";
import { PlatformStack } from "../lib/platform-stack";

describe("PlatformStack", () => {
    let template: Template;

    beforeAll(() => {
        const app = new cdk.App();
        const env = { account: "123456789012", region: "us-east-1" };

        const helperStack = new cdk.Stack(app, "HelperStack", { env });
        const rootZone = new route53.HostedZone(helperStack, "RootZone", {
            zoneName: "corderohq.com",
        });

        const stack = new PlatformStack(app, "TestPlatformStack", { env, rootZone });
        template = Template.fromStack(stack);
    });

    describe("User Pool — signup and sign-in", () => {
        test("self-signup is disabled", () => {
            template.hasResourceProperties("AWS::Cognito::UserPool", {
                AdminCreateUserConfig: {
                    AllowAdminCreateUserOnly: true,
                },
            });
        });

        test("sign-in uses email", () => {
            template.hasResourceProperties("AWS::Cognito::UserPool", {
                UsernameAttributes: ["email"],
            });
        });
    });

    describe("User Pool — MFA", () => {
        test("MFA is required", () => {
            template.hasResourceProperties("AWS::Cognito::UserPool", {
                MfaConfiguration: "ON",
            });
        });

        test("only TOTP MFA is enabled (no SMS)", () => {
            template.hasResourceProperties("AWS::Cognito::UserPool", {
                EnabledMfas: ["SOFTWARE_TOKEN_MFA"],
            });
        });
    });

    describe("User Pool — password policy", () => {
        test("minimum length is 8 characters", () => {
            template.hasResourceProperties("AWS::Cognito::UserPool", {
                Policies: { PasswordPolicy: { MinimumLength: 8 } },
            });
        });
    });

    describe("User Pool — device tracking", () => {
        test("requires challenge on new device", () => {
            template.hasResourceProperties("AWS::Cognito::UserPool", {
                DeviceConfiguration: {
                    ChallengeRequiredOnNewDevice: true,
                },
            });
        });

        test("device only remembered on user prompt", () => {
            template.hasResourceProperties("AWS::Cognito::UserPool", {
                DeviceConfiguration: {
                    DeviceOnlyRememberedOnUserPrompt: true,
                },
            });
        });
    });

    describe("User Pool — account recovery", () => {
        test("recovery uses email only", () => {
            template.hasResourceProperties("AWS::Cognito::UserPool", {
                AccountRecoverySetting: {
                    RecoveryMechanisms: [{ Name: "verified_email", Priority: 1 }],
                },
            });
        });
    });

    describe("User Pool — deletion protection", () => {
        test("deletion protection is active", () => {
            template.hasResourceProperties("AWS::Cognito::UserPool", {
                DeletionProtection: "ACTIVE",
            });
        });

        test("removal policy is RETAIN", () => {
            template.hasResource("AWS::Cognito::UserPool", {
                UpdateReplacePolicy: "Retain",
                DeletionPolicy: "Retain",
            });
        });
    });

    describe("User Pool — hosted UI domain", () => {
        test("custom domain is auth.corderohq.com", () => {
            template.hasResourceProperties("AWS::Cognito::UserPoolDomain", {
                CustomDomainConfig: {
                    CertificateArn: Match.anyValue(),
                },
            });
        });
    });

    describe("S3 assets bucket", () => {
        test("blocks all public access", () => {
            template.hasResourceProperties("AWS::S3::Bucket", {
                PublicAccessBlockConfiguration: {
                    BlockPublicAcls: true,
                    BlockPublicPolicy: true,
                    IgnorePublicAcls: true,
                    RestrictPublicBuckets: true,
                },
            });
        });

        test("uses S3-managed encryption", () => {
            template.hasResourceProperties("AWS::S3::Bucket", {
                BucketEncryption: {
                    ServerSideEncryptionConfiguration: Match.arrayWith([
                        Match.objectLike({
                            ServerSideEncryptionByDefault: { SSEAlgorithm: "AES256" },
                        }),
                    ]),
                },
            });
        });
    });

    describe("Shared ACM certificate", () => {
        test("covers the apex plus one wildcard per nesting level", () => {
            template.hasResourceProperties("AWS::CertificateManager::Certificate", {
                DomainName: "corderohq.com",
                SubjectAlternativeNames: [
                    "*.corderohq.com",
                    "auth.corderohq.com",
                    "*.hsa.corderohq.com",
                    "*.finance.corderohq.com",
                ],
            });
        });

        test("validates against the root zone", () => {
            template.hasResourceProperties("AWS::CertificateManager::Certificate", {
                DomainName: "corderohq.com",
                DomainValidationOptions: Match.arrayWith([Match.objectLike({ DomainName: "corderohq.com" })]),
            });
        });
    });

    describe("CloudFront distribution", () => {
        test("enforces HTTPS redirect", () => {
            template.hasResourceProperties("AWS::CloudFront::Distribution", {
                DistributionConfig: {
                    DefaultCacheBehavior: Match.objectLike({
                        ViewerProtocolPolicy: "redirect-to-https",
                    }),
                },
            });
        });

        test("has hsa.corderohq.com alias", () => {
            template.hasResourceProperties("AWS::CloudFront::Distribution", {
                DistributionConfig: Match.objectLike({
                    Aliases: ["hsa.corderohq.com"],
                }),
            });
        });
    });

    describe("Route 53 A records", () => {
        test("hsa.corderohq.com A record exists", () => {
            template.hasResourceProperties("AWS::Route53::RecordSet", {
                Name: "hsa.corderohq.com.",
                Type: "A",
            });
        });

        test("auth.corderohq.com A record exists", () => {
            template.hasResourceProperties("AWS::Route53::RecordSet", {
                Name: "auth.corderohq.com.",
                Type: "A",
            });
        });

        // Cognito makes a custom domain only when the parent domain resolves.
        test("apex A record exists, satisfying Cognito's parent-domain requirement", () => {
            template.hasResourceProperties("AWS::Route53::RecordSet", {
                Name: "corderohq.com.",
                Type: "A",
            });
        });

        test("every record lands in the root zone", () => {
            const records = template.findResources("AWS::Route53::RecordSet");
            const zoneIds = Object.values(records).map((r) => JSON.stringify(r.Properties.HostedZoneId));
            expect(new Set(zoneIds).size).toBe(1);
        });
    });
});
