# CareSlot Clinic Booking — complete consumer application

This is the **full consumer application** for the separately maintained `sigmoid_cdk` Python framework. It includes frontend, backend, infrastructure configuration, tests, GitHub Actions, local Docker DynamoDB and a CDK audit tool. Framework source is **not** copied into this repo; `requirements.txt` installs the published GitHub package.

## Application and services

- Patient interface: book, look up and cancel appointments; responsive HTML/CSS/JavaScript.
- Opening hours: Mon–Fri, 09:00–17:00 IST; closed 13:00–14:00 and on weekends; optional ISO-date holidays via `CLINIC_HOLIDAYS`.
- Backend: seven Python Lambda handlers in `src/backend/clinic.py`. The book operation uses DynamoDB conditional writes to prevent double booking.
- Cloud: CloudFront + private S3 for the site, API Gateway + Lambda, managed DynamoDB with `code-index`, SQS + DLQ, SES, EventBridge daily reminders.
- Local: **persistent DynamoDB Local Docker** (`ClinicBookingsLocal`), accessible only on `127.0.0.1:8001`; SQS and SES simulated for testing.
- No authentication for the public booking-reference operations; **demo only, not for actual patient data**. AWS staff route intentionally responds with 403 until staff auth is built.

## Project files

```text
clinic-booking-app-using-sigmoid-cdk/
├── .github/workflows/{ci,deploy,destroy}.yml
├── .gitignore
├── README.md
├── bin/
│   ├── app.py                  # CDK entry point
│   └── app_config.py           # ALL infrastructure declarations, including Lambdas
├── cdk.json
├── docker-compose.yml         # persistent DynamoDB Local plus safe volume init
├── requirements.txt
├── pytest.ini
├── scripts/review_cdk_output.py
├── src/
│   ├── backend/clinic.py       # seven Lambda handlers
│   └── frontend/
│       ├── index.html
│       ├── favicon.svg
│       └── assets/{app.js,styles.css}
├── tests/
└── tools/
    ├── local_server.py        # local HTTP server and inspector
    ├── fake_aws.py            # in-memory doubles (tests only)
    └── verify_stack.py
```

**The Lambda declarations belong only in `bin/app_config.py`.** The old `src/infrastructure/lambda_resources.py` is not needed. You can modify `DEFAULT_RUNTIME` or individual function overrides in `LAMBDA_RUNTIMES` without touching CDK constructs. If changing a Lambda to another language, supply matching code and set `LAMBDA_SOURCES`/`LAMBDA_HANDLERS` too. The existing handlers remain Python 3.14 in AWS.

To optionally add more private S3 buckets, change `ADDITIONAL_BUCKETS` in `bin/app_config.py` from `()` to `("reports", "private-assets")`. The default is one website bucket, matching the prior passing audit. More buckets are not created unless you request them.

## Local run: persistent DynamoDB

From this project's directory on Ubuntu/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt pytest

docker compose up -d
python tools/local_server.py
```

Website: http://127.0.0.1:8000

Developer inspector: http://127.0.0.1:8000/debug

DynamoDB endpoint: `http://127.0.0.1:8001`; table `ClinicBookingsLocal`.

DynamoDB Local writes to the **Docker named volume** `clinic_dynamodb_data`. The one-shot `dynamodb-volume-init` container fixes storage ownership before the database starts. **Do not run `docker compose down -v` or remove its volume** if you want to retain existing bookings. Replacing code does not copy/migrate/delete your Docker volume. Local data is separate from AWS DynamoDB.

Check real local records in the terminal:

```bash
AWS_ACCESS_KEY_ID=local AWS_SECRET_ACCESS_KEY=local AWS_DEFAULT_REGION=ap-south-1 \
aws dynamodb scan --table-name ClinicBookingsLocal \
--endpoint-url http://127.0.0.1:8001 --output table
```

Local emails are simulated and visible at `/debug` (not delivered to real addresses). To run isolated unit tests without connecting to Docker:

```bash
USE_DYNAMODB_LOCAL=0 python -m pytest -q
```

## Synthesis and validation

Install Node.js 22 or 24 and the AWS CDK CLI (`npm install -g aws-cdk`). Your `requirements.txt` pins the Python CDK version previously used to avoid the Python 3.15 preview mismatch and keeps the cdk-nag v2 import API. Do **not** suppress `AwsSolutions-L1` or use a preview runtime merely to pass an audit.

```bash
export STAGE=dev
export BUCKET_SUFFIX=demo
cdk synth
python scripts/review_cdk_output.py
```

Your previous working version completed **78 PASS / 0 FAIL / 4 WARN** with the corrected `sigmoid_cdk` framework. This restored application has minor configuration consolidation and an additional S3 audit check, so the total PASS count can differ. Always trust the fresh audit output, not an earlier result. The framework installed from GitHub **must include** the corrected per-Lambda DynamoDB and SES wiring (`TABLE_NAME_BOOKINGS`/`SENDER_EMAIL`).

Before AWS deployment set a globally unique S3 bucket suffix and a valid SES verified sender:

```bash
export AWS_REGION=ap-south-1
export AWS_DEFAULT_REGION=ap-south-1
export STAGE=dev
export BUCKET_SUFFIX="b$(aws sts get-caller-identity --query Account --output text)"
export SENDER_EMAIL='you@your-verified-domain.com'  # replace with YOUR verified identity
cdk synth
python scripts/review_cdk_output.py
cdk diff
```

Then check AWS identity, permissions and bootstrap before `cdk deploy`. CDK does not upload frontend files by itself; the GitHub deploy workflow syncs `src/frontend` to the provisioned website bucket and invalidates CloudFront. A successful synth is **not** proof of deployment. Do not deploy or use personal medical data before reviewing AWS settings and API security.

### GitHub workflows

- `ci.yml`: test, synth, audit on pushes to **main** and pull requests.
- `deploy.yml`: manual deploy for dev/staging/prod; GitHub secrets `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and the GitHub variables documented in `.github/workflows/deploy.yml`.
- `destroy.yml`: guarded dev/staging removal only, with backups; never triggers automatically.

**Git safety:** This ZIP does not contain `.git`, `.venv`, or `cdk.out`. Extract it into a NEW folder; do not run `git revert`, `git reset --hard`, or `git clean -fd` to recover application files. Review `git status` and `.gitignore` before your first commit. Your earlier uppercase GitHub `Main` branch and lowercase `main` branch are distinct; commit to **main** only after checking remote contents. If any current code differs, compare files rather than overwriting it.

## Limits

This is a well-structured demonstration, not a security-approved medical system. Booking-reference possession authorizes viewing/cancellation, staff API has no AWS authentication and therefore returns 403, SES sandbox restrictions may limit email, and asynchronous queue delivery can duplicate messages. Production needs authorization, protection of patient information, monitoring, deployment tests and compliance review. AWS resource usage can incur costs.
