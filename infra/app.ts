#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { DnsStack } from "./lib/dns-stack";
import { HsaReceiptsStack } from "./lib/hsa-receipts-stack";
import { HsaWebStack } from "./lib/hsa-web-stack";
import { MathQuizStack } from "./lib/math-quiz-stack";
import { PersonalFinanceDynamoDbStack } from "./lib/personal-finance-dynamodb-stack";
import { PersonalFinanceWebStack } from "./lib/personal-finance-web-stack";
import { PlatformStack } from "./lib/platform-stack";
import type { Stage } from "./lib/constants";

const app = new cdk.App();

const env = {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: "us-east-1",
};

// The single corderohq.com hosted zone. Deploy this stack before all the others. The shared
// certificate cannot complete its DNS validation until this zone is authoritative in public DNS.
const dns = new DnsStack(app, "DnsStack", { env, terminationProtection: true });

const platform = new PlatformStack(app, "PlatformStack", {
    env,
    terminationProtection: true,
    rootZone: dns.rootZone,
});
platform.addDependency(dns);

const hsaReceipts = new HsaReceiptsStack(app, "HsaReceiptArchiverStack", {
    env,
    terminationProtection: true,
    rootZone: dns.rootZone,
    alertTopic: platform.alertTopic,
});
hsaReceipts.addDependency(platform);

const hsaWeb = new HsaWebStack(app, "HsaWebStack", {
    env,
    terminationProtection: true,
    userPool: platform.userPool,
    assetsBucket: platform.assetsBucket,
    distribution: platform.distribution,
    dataBucket: hsaReceipts.bucket,
    processorFunction: hsaReceipts.handler,
    rootZone: dns.rootZone,
    certificate: platform.certificate,
    alertTopic: platform.alertTopic,
});
hsaWeb.addDependency(platform);
hsaWeb.addDependency(hsaReceipts);

// The math quiz is a static page only. It has no API, no sign-in, and no database.
const mathQuiz = new MathQuizStack(app, "MathQuizStack", {
    env,
    terminationProtection: true,
    rootZone: dns.rootZone,
    certificate: platform.certificate,
});
mathQuiz.addDependency(platform);

// The personal finance app. Make one set of stacks for each stage. The prod stacks have
// termination protection, so that a `cdk destroy` by mistake cannot remove the live data or
// the user pool client.
function createPersonalFinanceStacks(stage: Stage): void {
    const prefix = `PersonalFinance-${stage}`;
    const terminationProtection = stage === "prod";

    const data = new PersonalFinanceDynamoDbStack(app, `${prefix}-DynamoDbStack`, {
        env,
        stage,
        terminationProtection,
    });
    data.addDependency(platform);

    const web = new PersonalFinanceWebStack(app, `${prefix}-WebStack`, {
        env,
        stage,
        terminationProtection,
        userPool: platform.userPool,
        rootZone: dns.rootZone,
        certificate: platform.certificate,
        alertTopic: platform.alertTopic,
        categoryGroupTable: data.categoryGroupTable,
        categoryTable: data.categoryTable,
        budgetTable: data.budgetTable,
        budgetAuditLogTable: data.budgetAuditLogTable,
        transactionsTable: data.transactionsTable,
        profileTable: data.profileTable,
        accountTable: data.accountTable,
        netWorthSnapshotTable: data.netWorthSnapshotTable,
    });
    web.addDependency(platform);
    web.addDependency(data);
}

createPersonalFinanceStacks("prod");
createPersonalFinanceStacks("dev");
