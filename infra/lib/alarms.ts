import * as cdk from "aws-cdk-lib";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cloudwatchActions from "aws-cdk-lib/aws-cloudwatch-actions";
import type * as lambda from "aws-cdk-lib/aws-lambda";
import type * as sns from "aws-cdk-lib/aws-sns";
import type { Construct } from "constructs";

/**
 * Add an alarm that watches the error count of a Lambda function, and publish it to a topic.
 *
 * CloudWatch runs the alarm outside the function. So the alarm reports a failure that the
 * function code cannot report for itself. These failures all reach the alarm:
 *
 * - The handler module fails to import. No line of the function code runs.
 * - The function hits its timeout.
 * - The function runs out of memory.
 * - The error handler inside the function fails.
 *
 * `treatMissingData: NOT_BREACHING` holds the alarm in the OK state while the function is
 * idle. Without it, an idle function keeps the alarm in INSUFFICIENT_DATA. The alarm also
 * returns to OK after each error, so the next error raises a new notification.
 */
export function addLambdaErrorAlarm(
    scope: Construct,
    id: string,
    fn: lambda.IFunction,
    topic: sns.ITopic,
): cloudwatch.Alarm {
    const alarm = new cloudwatch.Alarm(scope, id, {
        alarmName: `${fn.functionName}-errors`,
        alarmDescription: `The ${fn.functionName} Lambda function reported an error.`,
        metric: fn.metricErrors({
            period: cdk.Duration.minutes(5),
            statistic: cloudwatch.Stats.SUM,
        }),
        threshold: 1,
        evaluationPeriods: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    alarm.addAlarmAction(new cloudwatchActions.SnsAction(topic));

    return alarm;
}
