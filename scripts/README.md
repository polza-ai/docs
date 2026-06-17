# upload_images_to_s3

Python script that scans a Mintlify docs repo (`.md`/`.mdx`), finds every local
image reference, uploads the file to S3-compatible storage (Yandex Object Storage
by default) with a **content-addressed key** (`<prefix>/<sha256>.<ext>`), and
rewrites the path in the source files to the public URL.

## Features

- Content-addressed (sha256) keys → **idempotent re-runs**, immutable `Cache-Control`.
- Persistent `assets-manifest.json` at the repo root so the bucket can be
  rebuilt from `repo + manifest` and re-runs do not re-upload unchanged files.
- Handles the two patterns actually used in this repo:
  - `<Frame><img src="/images/..." /></Frame>` (paired) — most common
  - `<Frame src="..." />` (self-close, src on Frame) — rare
  - `<Img src="..." />` — Mintlify alias
  - `![alt](/images/...)` — plain Markdown
  - `<Card imageSrc="..." />`, `<video src="...">` — included for completeness
- Skips `http://`, `https://`, `data:`, and protocol-relative `//cdn...` URLs.
- Resolves both repo-root-absolute paths (`/images/foo.png`) and file-relative
  paths (`../images/foo.png`).
- **Does not delete original images** from the repo (per project convention;
  git history + manifest are the source of truth).
- `--dry-run` / `--report` modes to inspect what would change before touching
  anything.

## Install

The script uses `boto3` only at upload time. Run it via `uv`:

```bash
uv run --with boto3 python scripts/upload_images_to_s3.py --report
```

For repeated runs, you can pin boto3 in a project-level `pyproject.toml` or just
keep using `uv run --with boto3`.

## Usage

### 1. See what would change (no side effects)

```bash
uv run --with boto3 python scripts/upload_images_to_s3.py --report
```

### 2. Dry-run (alias of `--report`)

```bash
uv run --with boto3 python scripts/upload_images_to_s3.py --dry-run
```

### 3. Real upload + rewrite

Set the env vars and run:

```bash
export S3_ENDPOINT="https://storage.yandexcloud.net"   # default
export S3_BUCKET="polza-ai-assets"
export S3_ACCESS_KEY_ID="..."
export S3_SECRET_ACCESS_KEY="..."
export S3_REGION="ru-central1"                          # default
export S3_KEY_PREFIX="polza-ai-docs/assets"             # default
# Optional: override the public base URL if the bucket is behind a CDN.
# export S3_PUBLIC_BASE="https://cdn.example/polza-ai-assets"
# Optional: skip per-object ACL if the bucket uses a bucket policy instead.
# export S3_NO_ACL=1

uv run --with boto3 python scripts/upload_images_to_s3.py
```

The script will:
1. Walk every `.md`/`.mdx` under the repo (excluding `.git/`, `node_modules/`,
   the `images/` directory itself, `openapi.json`, `favicon.ico`, `docs.json`).
2. For each image reference, resolve the local file. If it's `http(s)://` or
   `data:`, skip.
3. For each unique local file: compute sha256, check the manifest + S3 for an
   existing object. Upload if absent.
4. Rewrite the path token in every referencing file to the public URL.
5. Save `assets-manifest.json` at the repo root (commit it; it lets you
   rebuild the bucket from the repo + manifest after a disaster).

### Customization flags

```bash
--key-prefix polza-ai-docs/assets   # S3 key prefix (default)
--public-base https://cdn.example/...  # override public URL base
--dry-run / --scan                   # inspect only
--report                            # same as --dry-run
```

## Configuration reference

| Env var | Default | Required for upload? | Notes |
|---|---|---|---|
| `S3_ENDPOINT` | `https://storage.yandexcloud.net` | no | For MinIO, set e.g. `http://minio.local:9000`. |
| `S3_BUCKET` | — | **yes** | |
| `S3_REGION` | `ru-central1` | no | Leave empty for MinIO. |
| `S3_ACCESS_KEY_ID` | — | **yes** | |
| `S3_SECRET_ACCESS_KEY` | — | **yes** | |
| `S3_KEY_PREFIX` | `polza-ai-docs/assets` | no | |
| `S3_PUBLIC_BASE` | `<endpoint>/<bucket>` | no | Override when behind a CDN. |
| `S3_NO_ACL` | unset | no | Set to `1` to skip per-object ACLs (use bucket policy). |

## Manifest format (`assets-manifest.json`)

```json
{
  "version": 1,
  "endpoint": "https://storage.yandexcloud.net",
  "bucket": "polza-ai-assets",
  "public_base": "https://storage.yandexcloud.net/polza-ai-assets",
  "key_prefix": "polza-ai-docs/assets",
  "items": {
    "<sha256>": {
      "sha256": "<sha256>",
      "s3_key": "polza-ai-docs/assets/<sha256>.png",
      "public_url": "https://storage.yandexcloud.net/polza-ai-assets/polza-ai-docs/assets/<sha256>.png",
      "source_path": "images/n8n/n8n-search-openai.png",
      "first_seen_in": "integracii/n8n.mdx",
      "content_type": "image/png",
      "size": 12345,
      "uploaded_at": "2026-06-16T11:30:00+00:00"
    }
  }
}
```

`items` is keyed by sha256 for O(1) lookup. Re-running the script with no
changes to source files is a no-op: 0 uploads, 0 rewrites, manifest unchanged.

## Verification after a real run

```bash
# 1. No local image references left
grep -RIn '!\[' . --include='*.md' --include='*.mdx' \
  | grep -E '!\[[^]]*\]\(/images/' && echo "LEAKED MARKDOWN IMG"
grep -RIn '<Frame' . --include='*.mdx' \
  | grep -E 'src="/images/' && echo "LEAKED FRAME SRC"

# 2. Manifest size
jq '.items | length' assets-manifest.json

# 3. Spot-check one URL
curl -sI "$(jq -r '.items["<sha>"].public_url' assets-manifest.json)" | head

# 4. Mintlify link checker
mintlify broken-links
```

## Limitations

- Snippets inside `snippets/` are scanned (they're `.mdx`).
- `docs.json` icon references are **not** rewritten. If you reference local
  images there, patch them manually.
- `openapi.json` and `docs.json` are not scanned.
- HTML comments containing fake image paths are scanned (a few false positives
  may show up in `--report`; they get rewritten to a 404 URL on disk anyway,
  so the file is touched unnecessarily but the resulting URL is a broken link
  inside an HTML comment — harmless).
