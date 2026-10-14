import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as iam from "aws-cdk-lib/aws-iam";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as route53Targets from "aws-cdk-lib/aws-route53-targets";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment";
import * as sns from "aws-cdk-lib/aws-sns";
import type { Construct } from "constructs";
import { AUTH_DOMAIN, HSA_DOMAIN, PERSONAL_FINANCE_DOMAIN, ROOT_DOMAIN, WWW_DOMAIN } from "./constants";

interface PlatformStackProps extends cdk.StackProps {
    readonly rootZone: route53.IHostedZone;
}

/**
 * Shared platform infrastructure for all corderohq.com apps.
 *
 * AWS resources:
 * - ACM certificate: corderohq.com + *.corderohq.com + auth.corderohq.com + *.hsa.corderohq.com
 *   + *.finance.corderohq.com (DNS validated against the root zone, shared by every app)
 * - Cognito User Pool: corderohq-users (MFA required, TOTP, no self-signup)
 * - Cognito User Pool Domain: auth.corderohq.com (custom domain)
 * - Route 53 A record: auth.corderohq.com → Cognito
 * - S3 bucket: corderohq-assets (static web assets, shared across apps)
 * - CloudFront Function: directory-index-rewrite (URL rewriting for clean paths)
 * - CloudFront Function: www-to-apex-redirect (301 www.corderohq.com → corderohq.com)
 * - CloudFront distribution: corderohq.com + www.corderohq.com → S3 /root/ (OAC, HTTPS redirect)
 * - Route 53 A record: corderohq.com → CloudFront
 * - Route 53 A record: www.corderohq.com → CloudFront
 * - S3 BucketDeployment: web/root/ → assets bucket root/ prefix (CloudFront invalidation)
 * - CloudFront distribution: hsa.corderohq.com → S3 /hsa/ (OAC, HTTPS redirect, /oauth2/* → Cognito proxy)
 * - Route 53 A record: hsa.corderohq.com → CloudFront
 * - SNS topic: corderohq-alerts (CloudWatch alarms, handler stack traces, AWS budget alerts)
 * - SNS topic policy: budgets.amazonaws.com may publish to corderohq-alerts
 */
export class PlatformStack extends cdk.Stack {
    userPool!: cognito.UserPool;
    assetsBucket!: s3.Bucket;
    distribution!: cloudfront.Distribution;
    certificate!: acm.Certificate;
    readonly alertTopic: sns.Topic;
    private rootDistribution!: cloudfront.Distribution;

    constructor(scope: Construct, id: string, props: PlatformStackProps) {
        super(scope, id, props);

        cdk.Tags.of(this).add("project", "corderohq-platform");

        const { rootZone } = props;

        // One topic for every message that needs a developer: a CloudWatch alarm, a stack
        // trace from a handler, and an AWS budget alert. A single topic means a single email
        // subscription. The messages that a household member reads stay on their own topic,
        // `hsa-receipt-archiver-notifications`.
        this.alertTopic = new sns.Topic(this, "AlertTopic", {
            topicName: "corderohq-alerts",
            displayName: "corderohq alerts",
        });

        // AWS Budgets publishes to the topic directly, so the topic policy must name the
        // service. The budget itself stays in the stack of the app that it measures.
        this.alertTopic.addToResourcePolicy(
            new iam.PolicyStatement({
                actions: ["sns:Publish"],
                principals: [new iam.ServicePrincipal("budgets.amazonaws.com")],
                resources: [this.alertTopic.topicArn],
            }),
        );

        this.createCertificate(rootZone);
        this.createCloudFront();

        // Cognito makes a custom domain only when the parent domain resolves to an A record.
        // So corderohq.com must exist in the zone before we make auth.corderohq.com.
        const apexRecord = this.createApexRecord(rootZone);
        const cognitoDomain = this.createCognito();
        cognitoDomain.node.addDependency(apexRecord);

        this.createDnsRecords(rootZone, cognitoDomain);
    }

    /**
     * One certificate for every hostname across every app.
     *
     * A wildcard covers exactly one label, so each level that carries a hostname needs its own
     * entry: `*.corderohq.com` reaches hsa/auth/finance, `*.hsa.corderohq.com` reaches api.hsa,
     * and `*.finance.corderohq.com` reaches dev.finance. auth.corderohq.com is listed explicitly
     * even though the apex wildcard already matches it — Cognito is the fussiest consumer and the
     * one whose failure locks every app out, so it does not rely on wildcard matching.
     */
    private createCertificate(rootZone: route53.IHostedZone): void {
        this.certificate = new acm.Certificate(this, "SharedCertificate", {
            domainName: ROOT_DOMAIN,
            subjectAlternativeNames: [
                `*.${ROOT_DOMAIN}`,
                AUTH_DOMAIN,
                `*.${HSA_DOMAIN}`,
                `*.${PERSONAL_FINANCE_DOMAIN}`,
            ],
            validation: acm.CertificateValidation.fromDns(rootZone),
        });
    }

    private createCognito(): cognito.UserPoolDomain {
        this.userPool = new cognito.UserPool(this, "UserPool", {
            userPoolName: "corderohq-users",
            selfSignUpEnabled: false,
            signInAliases: { email: true },
            mfa: cognito.Mfa.REQUIRED,
            mfaSecondFactor: { sms: false, otp: true },
            passwordPolicy: {
                minLength: 8,
                requireLowercase: false,
                requireUppercase: false,
                requireDigits: false,
                requireSymbols: false,
                tempPasswordValidity: cdk.Duration.days(1),
            },
            deviceTracking: {
                challengeRequiredOnNewDevice: true,
                deviceOnlyRememberedOnUserPrompt: true,
            },
            accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
            removalPolicy: cdk.RemovalPolicy.RETAIN,
            deletionProtection: true,
        });

        return this.userPool.addDomain("CustomDomain", {
            customDomain: {
                domainName: AUTH_DOMAIN,
                certificate: this.certificate,
            },
        });
    }

    private createCloudFront(): void {
        // The S3 bucket for the static pages that PlatformStack serves: hsa/ and root/.
        // An app with a CloudFront distribution of its own needs a bucket of its own. Origin
        // access control writes the distribution ARN into the bucket policy, and that would make
        // this stack depend on the app stack.
        this.assetsBucket = new s3.Bucket(this, "AssetsBucket", {
            bucketName: `corderohq-assets-${this.account}-${this.region}`,
            blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
            encryption: s3.BucketEncryption.S3_MANAGED,
            removalPolicy: cdk.RemovalPolicy.RETAIN,
        });

        // A CloudFront function that rewrites a directory path, and a path with no extension.
        const urlRewriteCode = [
            "function handler(event) {",
            "    var request = event.request;",
            '    if (request.uri.endsWith("/")) {',
            '        request.uri += "index.html";',
            '    } else if (request.uri.lastIndexOf(".") <= request.uri.lastIndexOf("/")) {',
            '        request.uri += ".html";',
            "    }",
            "    return request;",
            "}",
        ].join("\n");

        const urlRewrite = new cloudfront.Function(this, "IndexRewriteFunction", {
            functionName: "directory-index-rewrite",
            code: cloudfront.FunctionCode.fromInline(urlRewriteCode),
        });

        // The apex serves one page, so it does not need the directory rewrite. It needs a
        // redirect instead: www.corderohq.com must send the visitor to the naked domain. A
        // behavior permits one viewer-request function only, so the apex gets a function of
        // its own.
        const wwwRedirectCode = [
            "function handler(event) {",
            "    var request = event.request;",
            `    if (request.headers.host.value === "${WWW_DOMAIN}") {`,
            "        return {",
            "            statusCode: 301,",
            '            statusDescription: "Moved Permanently",',
            `            headers: { location: { value: "https://${ROOT_DOMAIN}" + request.uri } }`,
            "        };",
            "    }",
            "    return request;",
            "}",
        ].join("\n");

        const wwwRedirect = new cloudfront.Function(this, "WwwRedirectFunction", {
            functionName: "www-to-apex-redirect",
            code: cloudfront.FunctionCode.fromInline(wwwRedirectCode),
        });

        // The apex distribution. It also gives corderohq.com an A record that resolves.
        // Cognito needs that record before it accepts auth.corderohq.com as a custom domain.
        this.rootDistribution = new cloudfront.Distribution(this, "RootDistribution", {
            comment: "corderohq.com apex",
            defaultBehavior: {
                origin: origins.S3BucketOrigin.withOriginAccessControl(this.assetsBucket, {
                    originPath: "/root",
                }),
                viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                functionAssociations: [
                    {
                        function: wwwRedirect,
                        eventType: cloudfront.FunctionEventType.VIEWER_REQUEST,
                    },
                ],
            },
            defaultRootObject: "index.html",
            // Our own error pages. A visitor then never sees the S3 error document in XML.
            //
            // Note which page a visitor really gets. Origin access control grants s3:GetObject
            // only, and not s3:ListBucket. Without the permission to list, S3 does not say
            // whether a key exists, so it answers 403 for a key that is absent. Almost every bad
            // path therefore reaches 403.html. 404.html shows only on a true 404 from the
            // origin.
            errorResponses: [
                { httpStatus: 403, responseHttpStatus: 403, responsePagePath: "/403.html" },
                { httpStatus: 404, responseHttpStatus: 404, responsePagePath: "/404.html" },
            ],
            priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
            domainNames: [ROOT_DOMAIN, WWW_DOMAIN],
            certificate: this.certificate,
        });

        new s3deploy.BucketDeployment(this, "RootAssets", {
            // `followSymlinks: EXTERNAL` resolves favicon.svg, which points at ../shared/.
            sources: [
                s3deploy.Source.asset("../web/root", {
                    followSymlinks: cdk.SymlinkFollowMode.EXTERNAL,
                }),
            ],
            destinationBucket: this.assetsBucket,
            destinationKeyPrefix: "root",
            distribution: this.rootDistribution,
            distributionPaths: ["/*"],
        });

        // The HSA distribution. It reads the assets bucket through origin access control.
        this.distribution = new cloudfront.Distribution(this, "Distribution", {
            comment: "corderohq.com web apps",
            defaultBehavior: {
                origin: origins.S3BucketOrigin.withOriginAccessControl(this.assetsBucket, {
                    originPath: "/hsa",
                }),
                viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                functionAssociations: [
                    {
                        function: urlRewrite,
                        eventType: cloudfront.FunctionEventType.VIEWER_REQUEST,
                    },
                ],
            },
            additionalBehaviors: {
                "/oauth2/*": {
                    origin: new origins.HttpOrigin(AUTH_DOMAIN),
                    viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
                    originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                    allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
                },
            },
            defaultRootObject: "index.html",
            priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
            domainNames: [HSA_DOMAIN],
            certificate: this.certificate,
        });
    }

    private createApexRecord(rootZone: route53.IHostedZone): route53.ARecord {
        const apexRecord = new route53.ARecord(this, "ApexCloudFrontAlias", {
            zone: rootZone,
            recordName: ROOT_DOMAIN,
            target: route53.RecordTarget.fromAlias(new route53Targets.CloudFrontTarget(this.rootDistribution)),
        });

        // The same distribution answers for www. Its viewer-request function then returns a
        // 301 to the naked domain.
        new route53.ARecord(this, "WwwCloudFrontAlias", {
            zone: rootZone,
            recordName: WWW_DOMAIN,
            target: route53.RecordTarget.fromAlias(new route53Targets.CloudFrontTarget(this.rootDistribution)),
        });

        return apexRecord;
    }

    private createDnsRecords(rootZone: route53.IHostedZone, cognitoDomain: cognito.UserPoolDomain): void {
        new route53.ARecord(this, "AuthCognitoAlias", {
            zone: rootZone,
            recordName: AUTH_DOMAIN,
            target: route53.RecordTarget.fromAlias(new route53Targets.UserPoolDomainTarget(cognitoDomain)),
        });

        new route53.ARecord(this, "HsaCloudFrontAlias", {
            zone: rootZone,
            recordName: HSA_DOMAIN,
            target: route53.RecordTarget.fromAlias(new route53Targets.CloudFrontTarget(this.distribution)),
        });
    }
}
