# Cadence 🪿

**A few hours a week, compounding.**

A reusable, self-hosted progress tracker for working through any structured goal with friends or colleagues. Drop in a CSV of items, configure your users and timeline, deploy to AWS, and get a shared dashboard with per-user checkboxes, progress tracking, and a countdown to completion.

Built for accountability. No paid subscriptions. Runs on AWS free tier.

## Use cases

- Certification study groups (AWS, CKA, CISSP, etc.)
- Monthly reading lists
- 30-day coding challenges
- Quarterly OKRs
- Any N-period goal with M collaborators

## Features

- **Any interval** — week, month, day, year, sprint, quarter
- **N users** — defined in config, each with their own login
- **Per-user checkboxes** — you can only check your own; others are read-only
- **Progress bars** — items completed + hours completed per user
- **Countdown** — days remaining to completion date
- **Persistent state** — stored in DynamoDB, survives page refreshes
- **Auth** — AWS Cognito email/password login
- **Static frontend** — no server, just S3 + CloudFront

---

## Prerequisites

Before you start, you need:

| Requirement | Notes |
|---|---|
| **AWS account** | [Create one free](https://aws.amazon.com/free/) |
| **AWS CLI v2** | [Install guide](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html) — run `aws configure` to set up credentials |
| **Python 3.11+** | `python3 --version` to check |
| **Linux or macOS** | Windows users: use [WSL2](https://learn.microsoft.com/en-us/windows/wsl/install) |

Your AWS credentials need sufficient permissions to create DynamoDB tables, Lambda functions, API Gateway APIs, Cognito User Pools, IAM roles, S3 buckets, and CloudFront distributions. An admin-level IAM user works; a scoped policy is better for production.

**Estimated AWS cost:** negligible. This project uses services well within the free tier:
- DynamoDB: 25GB storage + 200M requests/month free
- Lambda: 1M requests/month free
- S3: 5GB storage + 20k GET requests/month free
- CloudFront: 1TB data transfer + 10M requests/month free (first 12 months)
- Cognito: 50,000 MAUs free

---

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/rdyson/cadence.git
cd cadence

python3 -m venv .venv
source .venv/bin/activate      # Windows (WSL): same command
pip install pyyaml boto3
```

### 2. Configure

```bash
cp cadence.example.yaml cadence.yaml
cp items.example.csv items.csv
```

Open `cadence.yaml` and set:
- `name` — your project name
- `completion_date` — your target end date (ISO 8601: `YYYY-MM-DD`)
- `interval` — `week`, `month`, `day`, etc.
- `users` — one entry per person, with `id`, `name`, and `email`
- `aws.region` — your preferred AWS region (e.g. `eu-west-2`, `us-east-1`)

Open `items.csv` (or replace it with your own). The build script reads the column names from `cadence.yaml → columns`, so your CSV just needs a consistent header row.

### 3. Set up AWS infrastructure

```bash
bash scripts/setup-aws.sh
```

This creates all the AWS resources needed (~2 minutes):
- **DynamoDB** table for checkbox state
- **Lambda** function (the API)
- **API Gateway** (HTTP API with Cognito authorizer)
- **Cognito User Pool** with one user per person in `cadence.yaml`
- **S3 bucket** for the frontend

When it finishes, it writes the created resource IDs (Cognito pool ID, client ID, API URL, S3 bucket name) back into your `cadence.yaml` automatically. You'll see them appear in the `aws:` section.

### 4. Set up CloudFront

```bash
bash scripts/setup-cloudfront.sh
```

This creates a CloudFront distribution in front of your S3 bucket and writes the URL back to `cadence.yaml`. CloudFront provides HTTPS — required for Cognito auth to work correctly.

> **Why not just use S3 directly?** S3 static website URLs are HTTP only. Cognito requires HTTPS for authentication flows. CloudFront solves this and is free tier eligible.

Once the distribution is deployed (~5 minutes), the script prints your dashboard URL.

### 5. Build and deploy

```bash
python scripts/deploy.py
```

This:
1. Reads `cadence.yaml` + `items.csv` → builds `frontend/cadence.json`
2. Uploads all frontend files to your S3 bucket
3. Updates the Lambda function code
4. Invalidates the CloudFront cache

Your dashboard is now live at the CloudFront URL printed in step 4.

### 6. Sign in

Each user in `cadence.yaml` gets a Cognito account created with a **temporary password: `CadenceChange1!`**

On first sign-in, Cognito will prompt each user to set their own password. This is handled automatically by the login screen — they'll see a "Set new password" field appear after their first attempt.

Share the dashboard URL and temporary password with your collaborators.

---

## CSV format

Your CSV needs at minimum a title column and a period column. Hours are optional.

```csv
Title,Hours,Week
Introduction to the topic,0.5,1
Deep dive: subtopic A,2.0,1
Deep dive: subtopic B,1.5,2
```

Column names must match the `columns` settings in `cadence.yaml`. Defaults are `Title`, `Hours`, `Week`.

**Rows are automatically skipped if:**
- The title is blank
- The title starts with `--` (e.g. `-- Total hours` summary rows)
- The period value is not a valid integer (e.g. section header rows with no week number)

This means you can use a spreadsheet with section headers and totals — Cadence will ignore them cleanly.

---

## Config reference

See [`cadence.example.yaml`](cadence.example.yaml) for a fully annotated example.

| Field | Required | Description |
|---|---|---|
| `name` | ✅ | Project display name |
| `completion_date` | ✅ | Target end date (`YYYY-MM-DD`) |
| `interval` | ✅ | `week` / `month` / `day` / `year` / `sprint` / `quarter` |
| `csv` | ✅ | Path to your CSV (relative to `cadence.yaml`) |
| `columns.title` | ✅ | CSV column name for item titles |
| `columns.period` | ✅ | CSV column name for period numbers |
| `columns.hours` | ❌ | CSV column name for time estimates (omit to hide hours) |
| `users` | ✅ | List of `{ id, name, email }` |
| `period_labels` | ❌ | Override period headings (e.g. `1: "Week 1 — March 2"`) |
| `aws.region` | ✅ | AWS region |
| `aws.dynamodb_table` | ✅ | DynamoDB table name (set by setup script) |
| `aws.cognito_user_pool_id` | — | Set automatically by `setup-aws.sh` |
| `aws.cognito_client_id` | — | Set automatically by `setup-aws.sh` |
| `aws.api_url` | — | Set automatically by `setup-aws.sh` |
| `aws.s3_bucket` | — | Set automatically by `setup-aws.sh` |
| `aws.cloudfront_url` | — | Set automatically by `setup-cloudfront.sh` |

---

## Architecture

```
CloudFront (HTTPS)
      │
      ▼
S3 Bucket
  ├── index.html
  ├── app.js
  ├── style.css
  └── cadence.json  ← baked from cadence.yaml + items.csv at deploy time

      │ (JWT in Authorization header)
      ▼
API Gateway (Cognito JWT authorizer)
      │
      ▼
Lambda (lambda_function.py)
      │
      ▼
DynamoDB
  └── Table: one item per user, map of checked item titles
```

**How auth works:** Cognito issues a JWT on login. The browser includes it in every API request. API Gateway validates the token against your Cognito User Pool before the Lambda ever runs. The Lambda extracts the username from the validated claims — no auth logic in application code.

---

## Scripts

| Script | When to run | Description |
|---|---|---|
| `scripts/setup-aws.sh` | Once (first time) | Creates all AWS infrastructure |
| `scripts/setup-cloudfront.sh` | Once (first time) | Creates CloudFront distribution |
| `scripts/build.py` | After editing CSV/config | Builds `frontend/cadence.json` |
| `scripts/deploy.py` | After any changes | Build + upload to S3 + update Lambda |

`setup-aws.sh` and `setup-cloudfront.sh` are safe to re-run — they check for existing resources and skip them.

---

## Adding a new user

1. Add them to `users` in `cadence.yaml`
2. Run `bash scripts/setup-aws.sh` (skips existing resources, creates the new Cognito user)
3. Run `python scripts/deploy.py` (rebuilds `cadence.json` with the new user column)
4. Share the dashboard URL + temporary password `CadenceChange1!`

---

## Troubleshooting

**Login fails with "Incorrect username or password"**
The user may not have been created. Check that `setup-aws.sh` completed successfully and that the email in `cadence.yaml` matches what was used to create the Cognito user.

**Checkboxes don't save / API errors in console**
Check that `aws.api_url` is set in `cadence.yaml` (written by `setup-aws.sh`). Rebuild and redeploy: `python scripts/deploy.py`.

**Dashboard shows "Error loading cadence.json"**
Run `python scripts/build.py` to generate `frontend/cadence.json`, then redeploy.

**CloudFront returns stale content after deploy**
`deploy.py` creates a CloudFront invalidation automatically. If content still appears stale, wait 1–2 minutes for the invalidation to propagate.

**"Access Denied" from S3**
The S3 bucket is private by design. Traffic must go through CloudFront. Check that your CloudFront distribution has an Origin Access Control (OAC) set up pointing to the bucket — `setup-cloudfront.sh` handles this automatically.

---

## License

MIT
