# Setup: S3-compatible object storage (Cloudflare R2, AWS S3, MinIO)

## What it is for

ClipForge stores uploaded videos, extracted audio and exports through a
storage backend. By default (`STORAGE_BACKEND=local`) files are kept on the
server's disk under `backend/storage`. For production you can store them in an
S3-compatible bucket instead by setting `STORAGE_BACKEND=s3`. This also lets
the API hand out short-lived download links (presigned URLs) instead of
streaming files itself.

Nothing in this guide is needed for local development or for the automated
tests; the default `local` backend works without any account.

## Values you need

| Setting                | Description                                         |
| ---------------------- | --------------------------------------------------- |
| `STORAGE_BACKEND`      | `s3`                                                |
| `S3_ENDPOINT_URL`      | Provider S3 API URL (unset for AWS S3)              |
| `S3_REGION`            | Region (`auto` for R2)                              |
| `S3_BUCKET`            | Bucket name                                         |
| `S3_ACCESS_KEY_ID`     | Access key ID                                       |
| `S3_SECRET_ACCESS_KEY` | Secret access key (keep it secret)                  |
| `S3_ADDRESSING_STYLE`  | `path` for R2 and MinIO; `auto` for AWS S3          |

## Option A: Cloudflare R2 (recommended; has a free tier)

1. Sign in to the Cloudflare dashboard (<https://dash.cloudflare.com>) or
   create a free account.
2. In the left sidebar choose **R2 Object Storage**. If asked, enable R2 (the
   free tier includes a monthly storage and request allowance; a payment
   method may be required to activate it).
3. Click **Create bucket**, enter a name (for example `clipforge-media`) and
   create it. Keep the bucket **private** (do not enable public access).
   This name is `S3_BUCKET`.
4. On the R2 overview page note your **Account ID**. The endpoint is
   `https://<your-cloudflare-account-id>.r2.cloudflarestorage.com`; this is
   `S3_ENDPOINT_URL`.
5. Click **Manage R2 API Tokens** → **Create API token**. Choose the
   permission **Object Read & Write**, and under "Specify bucket(s)" select
   only your bucket. Create the token.
6. Copy the **Access Key ID** (`S3_ACCESS_KEY_ID`) and **Secret Access Key**
   (`S3_SECRET_ACCESS_KEY`). The secret is shown only once.
7. Use `S3_REGION=auto` and `S3_ADDRESSING_STYLE=path`.

## Option B: AWS S3

1. In the AWS console open **S3** → **Create bucket**. Pick a name
   (`S3_BUCKET`) and a region (`S3_REGION`, for example `eu-central-1`). Keep
   "Block all public access" enabled.
2. Open **IAM** → **Users** → **Create user**, then attach an inline policy
   that allows only `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` and
   `s3:AbortMultipartUpload` on `arn:aws:s3:::<your-bucket-name>/*`, and
   `s3:ListBucket` on `arn:aws:s3:::<your-bucket-name>` (needed so a missing
   object returns 404 instead of 403).
3. Under the user's **Security credentials** tab choose **Create access
   key**; copy the access key ID and secret access key.
4. Leave `S3_ENDPOINT_URL` unset and use `S3_ADDRESSING_STYLE=auto`.

The AWS free tier includes a small amount of S3 storage for new accounts.

## Option C: local MinIO (development only)

Start MinIO with `docker compose up -d` (see `docs/local-development.md`),
open the console at <http://localhost:9001>, sign in with the
development-only `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`, and create a
bucket named `clipforge`. `backend/.env.example` already contains the matching
`S3_*` values; set `STORAGE_BACKEND=s3` in `backend/.env` to use them.

## Where to put the values

- **Local machine:** in `backend/.env` (git-ignored). Copy it from
  `backend/.env.example` and replace the `S3_*` lines.
- **Deployment / CI:** as environment variables or GitHub repository secrets
  (Settings → Secrets and variables → Actions) used by the deployment
  workflow.

Never put real keys in `.env.example`, the README, issues, pull requests or
any other committed file.

## How to confirm it works

1. Set the values and `STORAGE_BACKEND=s3`, then run from `backend/`:

   ```bash
   python -c "import io; from app.storage import get_storage; s = get_storage(); s.save('setup-check/hello.txt', io.BytesIO(b'hello')); print(s.exists('setup-check/hello.txt'), s.get_download_url('setup-check/hello.txt', 300)); s.delete('setup-check/hello.txt')"
   ```

2. It should print `True` followed by a URL. Opening the URL in a browser
   within 5 minutes downloads `hello`.
3. If a setting is missing you get a `StorageConfigurationError` naming it.
   An `AccessDenied` or `SignatureDoesNotMatch` error means the keys or
   their permissions are wrong; `NoSuchBucket` means the bucket name or
   endpoint is wrong.
