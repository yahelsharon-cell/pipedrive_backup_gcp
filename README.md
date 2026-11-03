# Pipedrive CRM Backup

Daily backup of Pipedrive CRM data (deals, persons, organizations, notes, pipelines, stages) to Google Cloud Storage, run by GitHub Actions.

Each run produces `pipedrive_backup_YYYY-MM-DD_HHMMSS.zip` containing:

| Path | Purpose |
|---|---|
| `json/{endpoint}.json` | Complete raw API records for full-fidelity recovery |
| `json/fields/*Fields.json` | Custom-field definitions (hash key → name/options) |
| `csv/{deals,persons,organizations}.csv` | Flattened tables for Pipedrive's spreadsheet importer |
| `manifest.json` | Run metadata and record counts |

Authentication to GCP uses keyless OIDC (Workload Identity Federation) — no service-account keys are stored anywhere.

## Setup

### 1. GitHub repository secrets

*Settings → Secrets and variables → Actions*:

| Secret | Value |
|---|---|
| `PIPEDRIVE_API_TOKEN` | Pipedrive API token |
| `GCS_BUCKET_NAME` | Target bucket name (no `gs://`) |
| `WIF_PROVIDER` | `projects/<number>/locations/global/workloadIdentityPools/<pool>/providers/<provider>` |
| `WIF_SERVICE_ACCOUNT` | `<name>@<project>.iam.gserviceaccount.com` |

### 2. Google Cloud

- **Restrict the Workload Identity provider to this repository.** Without an attribute condition, workflows in *other* repositories could obtain tokens:
  ```
  assertion.repository == "<owner>/<repo>" && assertion.ref == "refs/heads/main"
  ```
- Grant the service account `roles/iam.workloadIdentityUser` for the pool principal of this repository.
- Grant the service account **only** `roles/storage.objectCreator` on the backup bucket (write-only: it cannot read or delete backups).
- On the bucket, enable object versioning or a retention policy, and a lifecycle rule to expire old backups.

### 3. GitHub repository settings (before making the repo public)

- *Settings → Code security*: enable **Secret scanning**, **Push protection** and **Dependabot alerts**.
- *Settings → Actions → General*: set workflow permissions to **Read repository contents**; require approval for fork pull request workflows from **all outside collaborators**.
- *Settings → Branches*: protect `main` (require pull requests, block force pushes).

## Running

- Scheduled daily at 02:00 UTC.
- Manually: *Actions → Pipedrive CRM Backup → Run workflow*.

Note: GitHub disables scheduled workflows in public repositories after 60 days without repository activity. Watch for that, or re-enable it from the Actions tab.

## Local run

```bash
pip install -r requirements.txt
export PIPEDRIVE_API_TOKEN=...      # never commit this
python backup.py
```

Optional environment variables: `PIPEDRIVE_BASE_URL`, `BACKUP_OUTPUT_DIR`, `LOG_LEVEL`.

## Security notes

- Backup archives contain personal data. They are `.gitignore`d — never commit them, and never add a step that uploads them as workflow artifacts or prints their contents to logs (logs and artifacts are public in a public repo).
- Third-party actions are pinned to full commit SHAs.
