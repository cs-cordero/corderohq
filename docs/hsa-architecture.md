# Architecture

## What this project does

This app keeps HSA (Health Savings Account) receipts. You can send a receipt in two ways:

1. **Email** — Send a photo or a PDF to a dedicated address. The app reads it, and then sends a
   notification with the result.
2. **Web UI** — Sign in, upload a receipt, and see the data at once. The web UI also has a ledger
   editor that looks like a spreadsheet.

The app does these steps for both ways:

1. It checks the receipt. For email, it also checks the email authentication.
2. It sends the receipt to Claude. Claude decides if the receipt is eligible.
3. It converts the receipt to PDF/A, a format that lasts many years.
4. It puts the PDF in S3, with a name that follows a set pattern.
5. It adds one row to a CSV ledger.
6. It reports the result. Email gets an SNS notification, and the web UI gets an HTTP response.

## Project layout

```
infra/                  AWS CDK infrastructure (TypeScript)
lambda/                 Lambda function code (Python 3.13)
    src/                Source code
    tests/              Unit tests
    scripts/            Utility scripts (bulk upload, dry run)
web/hsa/                Static web UI (HTML/CSS/JS)
docs/                   Documentation
```

## Infrastructure overview

Four CDK stacks hold this app. Deploy them in this order.

### DnsStack

The single hosted zone, `corderohq.com`. Every app records into it. Deploy this stack first. The
shared certificate cannot validate until this zone is authoritative in public DNS.

### PlatformStack

The resources that all apps share. It holds the TLS certificate, a Cognito user pool for sign-in,
an S3 bucket for static web assets, and a CloudFront distribution. The distribution serves the web
UI, and it rewrites URLs so that the paths stay clean.

### HsaReceiptArchiverStack

The email pipeline. It holds an S3 bucket for the receipt data, and the receipt Lambda. That Lambda
carries Ghostscript with it, for the PDF/A conversion. The stack also holds the SES receipt rules,
the SNS topics, the DNS records for mail, and a monthly budget with cost alerts.

### HsaWebStack

The web layer. It makes a Cognito app client, an API Gateway HTTP API that checks a JWT, and a
small Lambda for the web API. It also sends the static frontend to the shared assets bucket, and
then clears the CloudFront cache.

### How they connect

```
DnsStack (corderohq.com hosted zone — the single zone for every app)
    |
    +-- PlatformStack (shared TLS cert, Cognito, CloudFront, S3 assets)
            |
            +-- HsaReceiptArchiverStack (SES, receipt processor Lambda, data S3 bucket)
            |       |
            +-------+-- HsaWebStack (API Gateway, web Lambda, static site deployment)
```

The web stack depends on both of the other stacks. It uses the sign-in and the hosting from the
platform stack. It also calls the receipt Lambda, and it reads and writes the data bucket from the
receipt stack.

## Email flow

```
Email --> SES --> S3 (raw-emails/) + Lambda
                        |
                  The Lambda then:
                    1. Checks the email authentication (SPF/DKIM)
                    2. Compares the sender against an allow list
                    3. Sends each attachment to Claude
                    4. Converts each eligible receipt to PDF/A
                    5. Puts it in S3 (receipts/{year}/...)
                    6. Updates the CSV ledger
                    7. Sends an SNS notification
```

## Web flow

```
Browser --> CloudFront --> S3 (static HTML/CSS/JS)
                |
                +--> API Gateway --> Lambda (web handler)
                      (JWT auth)        |
                                   GET /ledger  --> read the CSV from S3
                                   PUT /ledger  --> write the CSV to S3
                                   POST /receipt --> call the processor Lambda
```

The web UI signs you in with the Cognito hosted page, and it uses PKCE. After you sign in, the
browser holds the tokens. It then sends them to the API as Bearer tokens.

## Source code structure

### `lambda/src/corderohq/`

```
receipt_handler.py      Receipt entrypoint (email + web uploads)
web_handler.py          Web API entrypoint (GET/PUT /ledger, POST /receipt)
claude_client.py        Claude API calls for HSA eligibility
util.py                 Shared helpers (get_env_var, parse_date, today)

aws/                    AWS service wrappers
    s3.py               S3 operations (receipts, ledger, raw emails)
    ses.py              SES email parsing and attachment extraction
    sns.py              SNS notifications (success, rejection, failure)
    ssm.py              SSM Parameter Store (API key, allowed senders)

archiver/               Core archive logic
    ledger.py           CSV ledger, with a check for duplicates
    pdf.py              PDF/A conversion and page extraction, through Ghostscript
```

### `web/hsa/`

```
index.html              Main page (ledger editor)
upload.html             Receipt upload page
callback.html           OAuth callback handler
config.js               Environment configuration (domains, client ID)
auth.js                 Sign-in and token management
ledger.js               Ledger editor, in the style of a spreadsheet (AG Grid)
upload.js               Receipt upload, and the result display
styles.css              Shared styles
favicon.svg             Symlink to web/shared/favicon.svg
```

### The receipt Lambda (`receipt_handler.py`)

`process_receipt` is the entrypoint. It accepts two kinds of event:

- **SES events**, from email. It reads the SES record, checks the SPF and DKIM verdicts, gets the
  raw email from S3, compares the sender against an allow list, and then reads each attachment.
- **Web events**, from an upload. It gets the file from S3 and reads it directly.

For each attachment, it sends the content to Claude. It then keeps only the eligible receipts,
unless force-store is on. It converts each one to PDF/A, puts it in S3, and updates the ledger.

### The web API (`web_handler.py`)

`handle` is the entrypoint for the HTTP API. It sends each request to a different function, by
method and path:

- **GET /ledger** — Returns the CSV ledger from S3. If no ledger exists, it returns an empty one.
- **PUT /ledger** — Replaces the CSV ledger in S3 with the request body.
- **POST /receipt** — Accepts a file in base64, puts it in S3, calls the processor Lambda, and
  waits. It then returns the entries and the rejections.

### Claude (`claude_client.py`)

The app uses Claude Haiku, because it costs less. The system prompt tells Claude to find each
out-of-pocket transaction on its own. Claude keeps what the patient pays — the copay, the
coinsurance, and the deductible. It ignores what the insurance pays, and it ignores adjustments.

Each result holds the eligibility, a description, a category, an amount, a provider, the dates,
the patient, and the reason.

A PDF of 3 to 10 pages becomes one image for each page, and Claude reads each page on its own.
The app rejects a PDF of more than 10 pages.

### PDF/A conversion (`archiver/pdf.py`)

Ghostscript makes the PDF/A-2b output, and the Lambda carries Ghostscript with it. Pillow converts
an image to a PDF first, at 300 DPI, and Ghostscript then converts that PDF. A PDF goes to
Ghostscript directly.

The build copies the Ghostscript shared libraries to `/var/task/lib`. That directory includes a
`libcrypto.so.3` from the build image. `pdf.py` puts the directory on `LD_LIBRARY_PATH` for the
Ghostscript subprocess only. A function-level `LD_LIBRARY_PATH` would make the Python `ssl`
module load that `libcrypto.so.3` in place of the runtime library. The two OpenSSL versions
differ, the import of `ssl` then fails, and the Lambda fails to import its handler module.

### The ledger (`archiver/ledger.py`)

One CSV file in S3. It has these columns: Id, Service Date, Payment Date, Vendor/Provider,
Patient/For, Category, Description, Amount, Receipt S3 URI, Reimbursed, Notes, Prob. of Duplicate.

Each new entry gets the next ID in sequence. It also gets a score from 0 to 100 for the
probability that it is a duplicate. The score compares the provider, the amount, and the service
date against the entries that exist.

## Environment variables

### Receipt processor Lambda

| Variable | Purpose |
|---|---|
| `BUCKET_NAME` | The S3 bucket for the receipt data |
| `SSM_API_KEY_PARAM` | The SSM path to the Anthropic API key |
| `SSM_ALLOWED_SENDERS_PARAM` | The SSM path to the allowed sender list |
| `SNS_TOPIC_ARN` | The main notification topic |
| `SNS_DETAILED_FAILURE_TOPIC_ARN` | The topic for detailed failures |

`LD_LIBRARY_PATH` and `GS_LIB` are not function environment variables. `pdf.py` sets them on
the Ghostscript subprocess only. See the note in the PDF/A conversion section.

### Web handler Lambda

| Variable | Purpose |
|---|---|
| `BUCKET_NAME` | The S3 bucket for the receipt data |
| `PROCESSOR_FUNCTION_NAME` | The receipt Lambda to call |

Every environment variable goes through `get_env_var()`. That function raises an error if the
value is absent or blank.

## Tests

- **Python** — 113 unit tests in 9 files. Every AWS call is a mock. Run `uv run pytest`.
- **TypeScript** — 47 CDK tests in 2 files. Run `npm test` from `infra/`.

The fixtures in `conftest.py` set dummy AWS credentials and SNS topic ARNs. Without them, the
`boto3.client()` calls at module level fail during the import.

## Key decisions

- **Four stacks** — The shared platform, the DNS, the receipt pipeline, and the web app each stay
  separate. You can then change one without the others.
- **The Haiku model** — It gives good results for a receipt, and it costs little.
- **PDF/A-2b** — A format made for storage that lasts many years.
- **A CSV ledger** — It is simple, it moves easily, and a person can edit it. No database is
  necessary.
- **One Claude call for each page** — A large document is too big for one call.
- **FORCE_STORE** — A way past the check, for when Claude rejects a good receipt.
- **SPF and DKIM checks** — They stop an attacker who makes a false email address.
- **S3 versions** — They protect the ledger if someone writes over it by mistake.
- **Ghostscript inside the Lambda** — It is part of the deployment, and not a Lambda layer. One
  fewer thing to manage.
- **The PKCE flow** — Safe OAuth in a browser, with no client secret.
- **A CloudFront function for the URLs** — It gives clean paths, such as `/upload` in place of
  `/upload.html`, and the server needs no route logic.
