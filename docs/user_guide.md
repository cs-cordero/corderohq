# User Guide

## Prerequisites

- The AWS CLI, set up with credentials for the target account
- Node.js, for the CDK
- Docker, because the CDK builds the Lambdas in a container during a deploy
- Python 3.13
- [uv](https://docs.astral.sh/uv/), the Python package manager

## Setup

```bash
# CDK dependencies
cd infra
npm install

# Lambda dependencies
cd lambda
uv sync

# Personal finance frontend dependencies
cd web/personal-finance
npm install
```

## Deploy

**Build the personal finance frontend first.** Its stack sends `web/personal-finance/dist` to S3,
and not the source. If you skip the build, the deploy sends the previous build again.

```bash
cd web/personal-finance && npm run build
cd ../../infra && npx cdk deploy --all
```

The other three frontends need no build step. Their stacks send the source folder directly.

The CDK does the rest. It builds the Lambdas, and that includes Ghostscript. It makes the AWS
resources, sends the web UI, and sets the permissions. The Lambda builds run in Docker, because
some dependencies need a compiler.

The stacks deploy in this order: `DnsStack`, then `PlatformStack`, then the app stacks. To deploy
one stack alone, and to leave the stacks it depends on as they are, use `--exclusively`:

```bash
npx cdk deploy HsaWebStack --exclusively
```

### First-time setup, after the first deploy

1. **Delegate the DNS from your registrar to Route 53.** Deploy `DnsStack` on its own first:

    ```bash
    npx cdk deploy DnsStack
    ```

    It makes the single `corderohq.com` hosted zone. Every app records into that zone. Copy its
    four nameservers from the `RootZoneNameServers` output into the nameserver settings at your
    registrar. Then wait until they resolve.

    Do this before you deploy anything else. The shared ACM certificate validates through DNS
    against this zone, so AWS cannot issue it until the delegation is live.

2. **Put the Anthropic API key in SSM Parameter Store:**
    ```bash
    aws ssm put-parameter \
        --name /hsa-receipt-archiver/anthropic-api-key \
        --value "sk-ant-..." \
        --type SecureString
    ```

3. **Set the allowed sender list.** Separate each address with a comma:
    ```bash
    aws ssm put-parameter \
        --name /hsa-receipt-archiver/allowed-senders \
        --value "you@example.com" \
        --type String
    ```

4. **Subscribe to the two SNS topics, to get the notifications by email.** The topics split by
   reader. Subscribe each person to the topic they can act on.

    ```bash
    # hsa-receipt-archiver-notifications — receipt archived, not eligible, or unreadable.
    # Plain words, no developer necessary. Subscribe every household member.
    aws sns subscribe \
        --topic-arn <notification-topic-arn> \
        --protocol email \
        --notification-endpoint you@example.com

    # corderohq-alerts — Lambda error alarms, Python stack traces, and AWS budget alerts.
    # Every app publishes here. Subscribe the developer only.
    aws sns subscribe \
        --topic-arn <alert-topic-arn> \
        --protocol email \
        --notification-endpoint you@example.com
    ```

    Find each topic ARN in the AWS Console under SNS, or in the output of `cdk deploy`. Each
    subscription sends a confirmation email. Click the link, or the topic stays silent.

5. **Make the SES receipt rule set active.** The CDK makes the rule set, but SES does not let the
   CDK activate it. Do this by hand in the AWS Console, under SES > Email Receiving > Rule Sets.

6. **Make a Cognito user.** The user pool does not permit self-signup, so make each user in the
   AWS Console or with the CLI:
    ```bash
    aws cognito-idp admin-create-user \
        --user-pool-id <pool-id> \
        --username you@example.com \
        --user-attributes Name=email,Value=you@example.com Name=email_verified,Value=true
    ```
    You get a temporary password by email. At the first sign-in, you set a new password and you
    set up MFA with TOTP.

## How to use the web UI

Open your domain in a browser. On the first visit, the app sends you to the sign-in page.

### The ledger editor

The home page shows the receipt ledger in a grid, in the style of a spreadsheet. AG Grid draws it.
You can:

- Change any cell directly
- Add a row
- Delete the rows you select
- Save your changes back to S3

The "Unsaved changes" mark appears when you have local changes that you did not save.

### How to upload a receipt

Click "Upload" in the navigation bar. Choose a receipt file — a PDF, a JPEG, a PNG, a GIF, or a
WebP — and then click "Upload & Analyze". The app then:

- Sends the receipt to Claude, which decides if it is eligible
- Shows the entries it found, in a table
- Shows each rejection, with the reason

Select "Force store" to go past the eligibility check. This helps when Claude rejects a good
receipt.

## How to send a receipt by email

Send an image or a PDF as an attachment, to your SES address. The app reads it, and then sends you
an SNS notification with the result.

To store a receipt that Claude rejects, send the email again with `FORCE_STORE` in the body.

Write `FORCE_STORE` in your own text, at the top. The app reads the new text only, and it ignores
the quoted reply chain below it. A quoted rejection notification holds the word `FORCE_STORE` in
its instructions, and the app must not read that as your decision. A reply works, as long as you
write the word above the quote.

`FORCE_STORE` goes past the eligibility check only. It does **not** go past the date check. The
app still rejects a receipt dated before 2025-01-27, because that date is earlier than the HSA
account.

## How to run the tests

```bash
# Python (113 tests)
cd lambda
uv run pytest              # every test
uv run pytest -v           # more output
uv run pytest tests/test_receipt_handler.py  # one file

# TypeScript CDK (47 tests)
cd infra
npm test
```

## Lint and format

### Python

```bash
cd lambda
uv run ruff check .        # lint
uv run ruff format .       # format
uv run ty check src/       # type check
```

### TypeScript

```bash
cd infra
npm run lint               # eslint
npm run format             # prettier
npm run typecheck          # tsc
```

### Personal finance frontend

```bash
cd web/personal-finance
npm run lint
npm run format:check
```

### The personal finance app: income, accounts, and uploads

The budget's top line is **left to assign**: the planned net income minus the budget targets. The
goal is `$0`. The summary page and the Allocations page show it.

1. **Set up the income.** On the Income page, add one income source for each stream of income
   that you want to plan, such as each job, or interest or dividends. For a job, type the gross
   amount and each deduction from a paystub. For a source with no deductions, type only the gross
   amount. The app calculates the net amount. Set the payments per month that the plan counts,
   usually 2 for pay every two weeks. The first payment goes in the current month and carries
   forward.
2. **Change a payment.** Change it in the month where the change starts. A change in the current
   month carries forward. A change in a future month is a pin.
3. **Add extra money.** For a bonus, an RSU payout, a third paycheck, or a DCFSA payout that you
   want to budget, add a one-time income line in that month. Extra money with no line is not part
   of the plan.
4. **Set up the accounts for uploads.** On the Accounts page, each account that takes a CSV upload
   needs the cash flow role "Spending" or "Holding". Checking and credit card accounts are "Spending"
   by default. Savings, brokerage, retirement, HSA, and 529 accounts are "Holding" by default. Add
   the last four digits of each account, so that the upload can find a transfer to it.
5. **Upload a CSV.** On the Upload page, choose the account of the file first. The review screen
   suggests a type for each row: Budget, Off-budget, or Skip. Look at the first list ("Look at
   these first"), change what is wrong, and commit. A row that you uploaded before starts
   unselected. If the signs of the amounts look wrong, click "Flip all signs".
6. **Mark a transaction as Off-budget** for work travel that your employer pays back, or for a
   payment with money from a savings fund. Add a note with the reason. The summary shows the
   Off-budget total below the Total.

## Scripts

### Move every liability to the debt asset class

Run this one time on each stage, after the code with the `debt` class and the `credit_card` type is
deployed to that stage. Without `--apply`, it only prints what it would change:

```bash
uv run python lambda/scripts/migrate_liabilities_to_debt.py --stage dev
uv run python lambda/scripts/migrate_liabilities_to_debt.py --stage dev --apply --to-credit-card <accountId>
```

It moves each mortgage and each other liability, and its net worth history, from the `other` class
to `debt`. `--to-credit-card` changes an `other_liability` account to a credit card. Repeat the
flag for each card. Get the account IDs from the Accounts page or the DynamoDB console.

### Dry run

See what Claude says about a receipt, and store nothing:

```bash
cd lambda
uv run python scripts/dry_run.py /path/to/receipt.jpg --api-key sk-ant-...
```

It prints what Claude would return: the eligibility, the amounts, the provider, the dates, and the
reason. Use it to test a change to the prompt, or to check a receipt before you send it. Add
`--model` to use a different model.

### Bulk upload

Send a folder of receipts through the whole Lambda pipeline. This helps when you move your old
receipts in for the first time:

```bash
cd lambda
uv run python scripts/bulk_upload.py /path/to/receipts/ \
    --bucket <bucket-name> \
    --sender you@example.com
```

The script puts each file in a MIME email, uploads that email to S3, and then calls the Lambda and
waits for it.

## Usual tasks

### How to read the Lambda logs

```bash
aws logs tail /aws/lambda/hsa-receipt-archiver --follow
aws logs tail /aws/lambda/hsa-web-handler --follow
```

### How to change an SSM parameter

```bash
# Change the allowed senders
aws ssm put-parameter \
    --name /hsa-receipt-archiver/allowed-senders \
    --value "user1@example.com,user2@example.com" \
    --type String \
    --overwrite

# Replace the API key
aws ssm put-parameter \
    --name /hsa-receipt-archiver/anthropic-api-key \
    --value "sk-ant-new-key" \
    --type SecureString \
    --overwrite
```

Note: the Lambda keeps each SSM value in its memory. After you change a parameter, the Lambda
reads the new value at its next cold start. To force a cold start, change any environment variable
on the Lambda. You can add a variable that does nothing, and then remove it.

### S3 tasks

```bash
# Get the ledger
aws s3 cp s3://BUCKET/ledger/hsa-receipts.csv ./hsa-receipts.csv

# List the receipts in the archive
aws s3 ls s3://BUCKET/receipts/ --recursive
```

## Costs

`corderohq-monthly-budget` sends an alert at 50%, at 80%, and at 100% of its $10 limit. The
budget has no cost filter, so it measures the whole AWS account and not one app. The alerts go
to the `corderohq-alerts` topic, together with the Lambda error alarms.

These four things cost the most:

- The Claude API calls. Anthropic bills these, and not AWS.
- The Lambda run time
- The S3 storage
- The SNS email delivery
