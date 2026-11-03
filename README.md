# Serverless Pipedrive CRM Backup Pipeline to Google Cloud Storage

An automated, zero-cost, ETL backup pipeline that exports Pipedrive CRM data, structures it for both technical disaster recovery and non-technical spreadsheet imports, and streams it into Google Cloud Storage (GCS) using GitHub Actions.

Built with a **zero-trust security model** using Google Cloud Workload Identity Federation (OIDC) — eliminating the need for static, long-lived service account keys.

---

## Architecture

```text
[ GitHub Actions (Daily Cron / Dispatch) ]
                    │
                    ▼
     [ Python 3.11 ETL Script ]
     ├── Authenticate with Pipedrive API (Header-based)
     ├── Extract CRM entities with pagination & auto-retry (HTTP 429)
     ├── Normalize custom fields & resolve option IDs to labels
     └── Package into dual-format ZIP:
         ├── json/  -> Complete raw payloads (Disaster Recovery)
         └── csv/   -> Flattened tables (Pipedrive UI Importer ready)
                    │
                    ▼
     [ Keyless GCP OIDC Authentication ]
     ├── Request short-lived token via Workload Identity Federation
     └── Impersonate least-privilege Service Account
                    │
                    ▼
    [ Google Cloud Storage (GCS) ]
     └── Standard Bucket (14-day retention via auto-delete lifecycle)
