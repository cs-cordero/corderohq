import * as cdk from "aws-cdk-lib";
import * as budgets from "aws-cdk-lib/aws-budgets";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as ses from "aws-cdk-lib/aws-ses";
import * as sesActions from "aws-cdk-lib/aws-ses-actions";
import * as sns from "aws-cdk-lib/aws-sns";
import type { Construct } from "constructs";
import { HSA_DOMAIN } from "./constants";

interface HsaReceiptsStackProps extends cdk.StackProps {
    readonly rootZone: route53.IHostedZone;
}

/**
 * HSA receipt processing via email (SES inbound).
 *
 * AWS resources:
 * - S3 bucket: hsa-receipts (versioned, lifecycle rules for raw-emails/)
 * - CloudWatch log group: /aws/lambda/hsa-receipt-archiver
 * - SNS topic: hsa-receipt-archiver-notifications
 * - SNS topic: hsa-receipt-archiver-detailed-failures
 * - Lambda function: hsa-receipt-archiver (Python 3.13, Ghostscript bundled)
 * - IAM policy: S3 read/write, SSM GetParameter, SNS Publish
 * - SES receipt rule set: hsa-receipt-archiver
 * - SES receipt rule: receipts@hsa.corderohq.com → S3 + Lambda
 * - Route 53 MX record: hsa.corderohq.com → inbound-smtp.us-east-1.amazonaws.com
 * - Route 53 TXT record: _amazonses.hsa.corderohq.com (SES domain verification)
 * - SNS topic: hsa-receipt-archiver-budget-alerts
 * - Budget: hsa-receipt-archiver-monthly ($10 USD, alerts at 50/80/100%)
 */
export class HsaReceiptsStack extends cdk.Stack {
    readonly bucket: s3.Bucket;
    readonly handler: lambda.Function;

    constructor(scope: Construct, id: string, props: HsaReceiptsStackProps) {
        super(scope, id, props);

        cdk.Tags.of(this).add("project", "hsa-receipt-archiver");

        // S3 Bucket
        this.bucket = new s3.Bucket(this, "ReceiptsBucket", {
            bucketName: `hsa-receipts-${this.account}-${this.region}`,
            blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
            encryption: s3.BucketEncryption.S3_MANAGED,
            versioned: true,
            removalPolicy: cdk.RemovalPolicy.RETAIN,
            lifecycleRules: [
                {
                    prefix: "raw-emails/",
                    tagFilters: { status: "processed" },
                    expiration: cdk.Duration.days(7),
                },
                {
                    prefix: "raw-emails/",
                    expiration: cdk.Duration.days(30),
                },
            ],
        });

        // CloudWatch Log Group
        const logGroup = new logs.LogGroup(this, "LambdaLogGroup", {
            logGroupName: "/aws/lambda/hsa-receipt-archiver",
            retention: logs.RetentionDays.ONE_MONTH,
            removalPolicy: cdk.RemovalPolicy.DESTROY,
        });

        // SNS Notification Topic
        const notificationTopic = new sns.Topic(this, "NotificationTopic", {
            topicName: "hsa-receipt-archiver-notifications",
        });

        // SNS Detailed Failure Topic
        const detailedFailureTopic = new sns.Topic(this, "DetailedFailureTopic", {
            topicName: "hsa-receipt-archiver-detailed-failures",
        });

        // Lambda Function
        this.handler = new lambda.Function(this, "ReceiptArchiver", {
            functionName: "hsa-receipt-archiver",
            runtime: lambda.Runtime.PYTHON_3_13,
            handler: "corderohq.receipt_handler.process_receipt",
            code: lambda.Code.fromAsset("../lambda", {
                bundling: {
                    image: lambda.Runtime.PYTHON_3_13.bundlingImage,
                    user: "root",
                    command: [
                        "bash",
                        "-c",
                        [
                            "dnf install -y ghostscript",
                            "pip install -r requirements.txt -t /asset-output",
                            "cp -r src/corderohq /asset-output/",
                            "mkdir -p /asset-output/bin /asset-output/lib",
                            "cp /usr/bin/gs /asset-output/bin/gs",
                            "ldd /usr/bin/gs | awk '/=>/ {print $3}' | xargs -I{} cp {} /asset-output/lib/",
                            "cp -rL /usr/share/ghostscript /asset-output/share/",
                        ].join(" && "),
                    ],
                },
            }),
            memorySize: 1024,
            timeout: cdk.Duration.minutes(5),
            logGroup,
            // `LD_LIBRARY_PATH` and `GS_LIB` are absent on purpose. The Ghostscript libraries
            // in `/var/task/lib` include a `libcrypto.so.3` from the build image. A
            // function-level `LD_LIBRARY_PATH` makes the Python `ssl` module load that library
            // in place of the runtime library, and the import of `ssl` then fails. `pdf.py`
            // sets both variables on the Ghostscript subprocess instead.
            environment: {
                BUCKET_NAME: this.bucket.bucketName,
                SNS_TOPIC_ARN: notificationTopic.topicArn,
                SNS_DETAILED_FAILURE_TOPIC_ARN: detailedFailureTopic.topicArn,
                SSM_API_KEY_PARAM: "/hsa-receipt-archiver/anthropic-api-key",
                SSM_ALLOWED_SENDERS_PARAM: "/hsa-receipt-archiver/allowed-senders",
            },
        });

        // IAM Permissions
        this.bucket.grantReadWrite(this.handler);

        this.handler.addToRolePolicy(
            new iam.PolicyStatement({
                actions: ["ssm:GetParameter"],
                resources: [
                    cdk.Arn.format(
                        {
                            service: "ssm",
                            resource: "parameter",
                            resourceName: "hsa-receipt-archiver/*",
                        },
                        this,
                    ),
                ],
            }),
        );

        notificationTopic.grantPublish(this.handler);
        detailedFailureTopic.grantPublish(this.handler);

        // SES Receipt Rule Set + Rule
        const ruleSet = new ses.ReceiptRuleSet(this, "ReceiptRuleSet", {
            receiptRuleSetName: "hsa-receipt-archiver",
        });

        ruleSet.addRule("ReceiptRule", {
            recipients: [`receipts@${HSA_DOMAIN}`],
            actions: [
                new sesActions.S3({
                    bucket: this.bucket,
                    objectKeyPrefix: "raw-emails/",
                }),
                new sesActions.Lambda({
                    function: this.handler,
                }),
            ],
        });

        this.createDnsRecords(props.rootZone);
        this.createBudgetAlerts();
    }

    private createDnsRecords(rootZone: route53.IHostedZone): void {
        // The SES MX record in Route 53. The record name is explicit, because the zone is now
        // corderohq.com. Without it, the record would go to the zone apex. SES would then take
        // corderohq.com, and it would write over the mail records there.
        new route53.MxRecord(this, "SesMxRecord", {
            zone: rootZone,
            recordName: HSA_DOMAIN,
            values: [{ priority: 10, hostName: "inbound-smtp.us-east-1.amazonaws.com" }],
        });

        // SES domain verification TXT record
        new route53.TxtRecord(this, "SesTxtRecord", {
            zone: rootZone,
            recordName: `_amazonses.${HSA_DOMAIN}`,
            values: ["YonxACp7We9tgvwNDh9cZQtqb5HkmUuxn0NGRnWIRqk="],
        });
    }

    private createBudgetAlerts(): void {
        // Budget Alerts via SNS
        const budgetTopic = new sns.Topic(this, "BudgetAlertsTopic", {
            topicName: "hsa-receipt-archiver-budget-alerts",
        });

        budgetTopic.addToResourcePolicy(
            new iam.PolicyStatement({
                actions: ["sns:Publish"],
                principals: [new iam.ServicePrincipal("budgets.amazonaws.com")],
                resources: [budgetTopic.topicArn],
            }),
        );

        const budgetThresholdPercentages = [50, 80, 100];

        new budgets.CfnBudget(this, "MonthlyBudget", {
            budget: {
                budgetName: "hsa-receipt-archiver-monthly",
                budgetType: "COST",
                timeUnit: "MONTHLY",
                budgetLimit: {
                    amount: 10,
                    unit: "USD",
                },
            },
            notificationsWithSubscribers: budgetThresholdPercentages.map((pct) => ({
                notification: {
                    notificationType: "ACTUAL",
                    comparisonOperator: "GREATER_THAN",
                    threshold: pct,
                    thresholdType: "PERCENTAGE",
                },
                subscribers: [
                    {
                        subscriptionType: "SNS",
                        address: budgetTopic.topicArn,
                    },
                ],
            })),
        });
    }
}
