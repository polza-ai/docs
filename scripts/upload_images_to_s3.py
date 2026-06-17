#!/usr/bin/env python3
"""
Upload local images referenced in .md/.mdx to S3-compatible storage (Yandex Object Storage
by default) and rewrite the references to public URLs.

What it does
------------
1. Scans every .md and .mdx file under the repo (excluding .git, node_modules, the images/
   directory itself, the OpenAPI spec, and a few Mintlify internals).
2. Finds image references in two forms used by this repo:
   - Mintlify ``<Frame>`` wrapping an HTML ``<img src="..." />`` (with or without
     surrounding whitespace, paired and self-closing forms)
   - Vanilla Markdown ``![alt](path)``
3. Resolves each path against the repo root. Absolute site paths (``/images/...``) and
   relative paths (``../images/foo.png``) are both handled.
4. Skips references that are already ``http(s)://`` or ``data:`` URIs.
5. Hashes each local file with sha256, uploads it to ``<bucket>/<prefix>/<sha256><ext>``
   with correct ``Content-Type`` and an immutable ``Cache-Control`` header, and rewrites
   the path inside the .md/.mdx to the public URL.
6. Persists an ``assets-manifest.json`` at the repo root keyed by sha256 so re-runs are
   idempotent and S3 can be rebuilt from repo + manifest.

Modes
-----
* default        - upload + rewrite (only uploads files that aren't already in the manifest
  with a live S3 object).
* ``--dry-run``  - report what would change; do not upload, do not write any files.
* ``--scan``     - alias of ``--dry-run``.
* ``--report``   - print a summary table and exit before any side effects.

Configuration (env vars, all optional except S3_BUCKET/S3_ACCESS_KEY_ID/S3_SECRET_ACCESS_KEY
for the real run):
    S3_ENDPOINT         default https://storage.yandexcloud.net
    S3_BUCKET           required
    S3_REGION           default ru-central1
    S3_ACCESS_KEY_ID    required
    S3_SECRET_ACCESS_KEY required
    S3_KEY_PREFIX       default polza-ai-docs/assets
    S3_PUBLIC_BASE      default <endpoint>/<bucket>  (override if behind a CDN)
    S3_NO_ACL=1         skip per-object ACL (use when the bucket has a public policy
                        instead of ACLs; YC buckets since 2024 default-deny public ACLs)

Pitfalls handled
----------------
* Re-runs are safe: content-addressed keys + manifest + head_object check.
* The original images are NOT removed from the repo; the manifest is the source of truth
  and git history is the safety net.
* The wrapper tag (<Frame>, <Img>, the ![alt] part of a Markdown image, the alt text,
  and any title) is preserved; only the path token is replaced.
* Relative paths like ``../images/foo.png`` are resolved against the referencing file.
* ``openapi.json``, ``docs.json``, and ``favicon.ico`` are not touched.

Run via: uv run --with boto3 python scripts/upload_images_to_s3.py [--dry-run] [--report]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# Lazy import for boto3 - the script can do --report/--dry-run without it.
try:
    import boto3
    from botocore.client import Config
    from botocore.exceptions import ClientError
except ImportError:  # boto3 is installed via `uv run --with boto3`
    boto3 = None
    Config = None
    ClientError = Exception

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "assets-manifest.json"
MANIFEST_VERSION = 1

EXCLUDE_DIRS = {".git", "node_modules", "images", ".mintlify", "mintlify-build"}
EXCLUDE_FILES = {"openapi.json", "favicon.ico", "docs.json"}

# --- regex patterns ---------------------------------------------------------
# Captures: the path token to rewrite.
#
# Forms actually used in this repo (verified by grep):
#   <Frame>
#     <img src="/images/.../foo.png" alt="..." />
#   </Frame>
#   <Frame src="/images/.../foo.png" />           (rare; kept for completeness)
#   <Img src="..." alt="..." />                    (Mintlify alias of Frame)
#   ![alt](/images/.../foo.png)                    (plain Markdown)

# Match the opening <Frame ...> tag (so we can find the matching img inside).
_FRAME_OPEN_RE = re.compile(r"<Frame\b[^>]*>", re.IGNORECASE)
_FRAME_CLOSE_RE = re.compile(r"</Frame\s*>", re.IGNORECASE)
_FRAME_SRC_RE = re.compile(
    r"<Frame\b[^>]*\bsrc=([\"\'])([^\"\']+)\1", re.IGNORECASE
)
_IMG_TAG_RE = re.compile(r"<Img\b[^>]*\/?>", re.IGNORECASE)
_IMG_TAG_SRC_RE = re.compile(r"src=([\"\'])([^\"\']+)\1", re.IGNORECASE)
_IMG_RE = re.compile(
    r"<img\b[^>]*\bsrc=([\"\'])([^\"\']+)\1[^>]*\/?>", re.IGNORECASE
)
_CARD_RE = re.compile(
    r"<Card\b[^>]*\bimageSrc=([\"\'])([^\"\']+)\1[^>]*\/?>", re.IGNORECASE
)
_VIDEO_RE = re.compile(
    r"<video\b[^>]*\bsrc=([\"\'])([^\"\']+)\1", re.IGNORECASE
)
# Markdown image: ![alt](path "optional title")
_MD_IMG_RE = re.compile(
    r"!\[([^\]]*)\]\(([^\s\)]+)(?:\s+\"[^\"]*\")?\)"
)
# Combined "does this file mention any image-bearing construct?" quick check.
_HAS_IMAGE_RE = re.compile(
    r"<Frame\b|<Img\b|<img\b|<Card\b|<video\b|!\[[^\]]*\]\(",
    re.IGNORECASE,
)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico"}


@dataclass
class ImageRef:
    """A single image reference inside a .md/.mdx file."""
    file: Path            # absolute path to the .md/.mdx
    repo_path: str        # relative to REPO_ROOT (for reports)
    rel: str              # the original path token as it appears in the source
    abs_path: Path | None # resolved absolute path on disk (None if missing)
    sha256: str | None    # computed lazily
    public_url: str | None
    matched_pattern: str  # "frame-img", "frame-src", "img", "markdown", "card", "video"


# --- path resolution --------------------------------------------------------

def _is_remote(rel: str) -> bool:
    s = rel.strip()
    return (
        s.startswith("http://")
        or s.startswith("https://")
        or s.startswith("data:")
        or s.startswith("//")  # protocol-relative
    )


def resolve_path(reference_file: Path, rel: str) -> Path | None:
    """Resolve a path token to an absolute path on disk, or None if external/empty."""
    s = rel.strip()
    if not s or _is_remote(s):
        return None
    if s.startswith("/"):
        return (REPO_ROOT / s.lstrip("/")).resolve()
    # relative to referencing file's directory
    return (reference_file.parent / s).resolve()


# --- file discovery ---------------------------------------------------------

def iter_md_files() -> Iterable[Path]:
    for p in sorted(REPO_ROOT.rglob("*.md")):
        if _is_excluded(p):
            continue
        yield p
    for p in sorted(REPO_ROOT.rglob("*.mdx")):
        if _is_excluded(p):
            continue
        yield p


def _is_excluded(p: Path) -> bool:
    rel = p.relative_to(REPO_ROOT)
    if any(part in EXCLUDE_DIRS for part in rel.parts):
        return True
    if rel.name in EXCLUDE_FILES:
        return True
    return False


# --- reference extraction ---------------------------------------------------

def _looks_like_image(rel: str) -> bool:
    return any(rel.lower().endswith(ext) for ext in IMAGE_EXTS)


def extract_refs(file: Path) -> list[ImageRef]:
    """Find all image references in a single .md/.mdx file."""
    try:
        text = file.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = file.read_text(encoding="utf-8", errors="replace")

    if not _HAS_IMAGE_RE.search(text):
        return []

    refs: list[ImageRef] = []
    seen: set[tuple[str, str]] = set()

    def add(rel: str, pattern: str) -> None:
        if not rel or _is_remote(rel):
            return
        if not _looks_like_image(rel):
            return
        key = (rel, pattern)
        if key in seen:
            return
        seen.add(key)
        abs_path = resolve_path(file, rel)
        if abs_path is None or not abs_path.exists() or not abs_path.is_file():
            refs.append(ImageRef(
                file=file,
                repo_path=str(file.relative_to(REPO_ROOT)),
                rel=rel,
                abs_path=None,
                sha256=None,
                public_url=None,
                matched_pattern=pattern,
            ))
            return
        refs.append(ImageRef(
            file=file,
            repo_path=str(file.relative_to(REPO_ROOT)),
            rel=rel,
            abs_path=abs_path,
            sha256=None,
            public_url=None,
            matched_pattern=pattern,
        ))

    # 1. <Frame>...<img src="...">...</Frame>
    for m in _FRAME_OPEN_RE.finditer(text):
        block_start = m.end()
        end_match = _FRAME_CLOSE_RE.search(text, block_start)
        end = end_match.start() if end_match else len(text)
        for img_m in _IMG_RE.finditer(text, block_start, end):
            add(img_m.group(2), "frame-img")

    # 2. <Frame src="..." />  (src on the Frame tag itself)
    for m in _FRAME_SRC_RE.finditer(text):
        add(m.group(2), "frame-src")

    # 3. <Img src="..." />
    for m in _IMG_TAG_RE.finditer(text):
        sm = _IMG_TAG_SRC_RE.search(m.group(0))
        if sm:
            add(sm.group(2), "img")

    # 4. <Card imageSrc="..." />
    for m in _CARD_RE.finditer(text):
        add(m.group(2), "card")

    # 5. <video src="...">
    for m in _VIDEO_RE.finditer(text):
        add(m.group(2), "video")

    # 6. Markdown ![alt](path)
    for m in _MD_IMG_RE.finditer(text):
        path = m.group(2)
        if path and not _is_remote(path) and _looks_like_image(path):
            add(path, "markdown")

    return refs


# --- S3 upload --------------------------------------------------------------

def make_s3_client(endpoint: str, key_id: str, secret: str, region: str):
    if boto3 is None:
        raise SystemExit(
            "boto3 is required for upload. Run with `uv run --with boto3 python "
            "scripts/upload_images_to_s3.py`."
        )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        region_name=region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        try:
            m = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            if m.get("version") != MANIFEST_VERSION:
                print(
                    f"WARNING: manifest version {m.get('version')} != {MANIFEST_VERSION}; "
                    "starting fresh.",
                    file=sys.stderr,
                )
                return _empty_manifest()
            return m
        except (json.JSONDecodeError, KeyError) as e:
            print(f"WARNING: failed to parse manifest ({e}); starting fresh.", file=sys.stderr)
    return _empty_manifest()


def _empty_manifest() -> dict:
    return {
        "version": MANIFEST_VERSION,
        "endpoint": os.environ.get("S3_ENDPOINT", "https://storage.yandexcloud.net"),
        "bucket": os.environ.get("S3_BUCKET", ""),
        "public_base": os.environ.get("S3_PUBLIC_BASE", ""),
        "key_prefix": os.environ.get("S3_KEY_PREFIX", "polza-ai-docs/assets"),
        "items": {},
    }


def save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def object_exists(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def upload_image(
    s3,
    bucket: str,
    key_prefix: str,
    abs_path: Path,
    *,
    public: bool = True,
) -> tuple[str, str, str, int]:
    """Upload a single file. Returns (sha256, s3_key, content_type, size)."""
    data = abs_path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    ext = abs_path.suffix.lower()
    key = f"{key_prefix.rstrip('/')}/{sha}{ext}"
    ct = mimetypes.guess_type(abs_path.name)[0] or "application/octet-stream"
    extra: dict = {
        "ContentType": ct,
        "CacheControl": "public, max-age=31536000, immutable",
    }
    if public and not os.environ.get("S3_NO_ACL"):
        extra["ACL"] = "public-read"
    s3.put_object(Bucket=bucket, Key=key, Body=data, **extra)
    return sha, key, ct, len(data)


# --- file rewrite -----------------------------------------------------------

def rewrite_file(file: Path, rel_to_url: dict[str, str]) -> int:
    """Rewrite image path tokens in `file` to public URLs. Returns count of rewrites."""
    text = file.read_text(encoding="utf-8")
    original = text
    n = 0

    def sub_token(regex: re.Pattern, path_index: int) -> None:
        nonlocal text, n
        def repl(m: re.Match) -> str:
            nonlocal n
            path = m.group(path_index)
            if path in rel_to_url:
                new = m.group(0).replace(path, rel_to_url[path], 1)
                if new != m.group(0):
                    n += 1
                return new
            return m.group(0)
        text = regex.sub(repl, text)

    sub_token(_IMG_RE, 2)            # <img src="path" ...>
    sub_token(_FRAME_SRC_RE, 2)      # <Frame src="path" ...>
    sub_token(_CARD_RE, 2)           # <Card imageSrc="path" .../>
    sub_token(_VIDEO_RE, 2)          # <video src="path">

    def md_repl(m: re.Match) -> str:
        nonlocal n
        path = m.group(2)
        if path in rel_to_url:
            new = m.group(0).replace(path, rel_to_url[path], 1)
            if new != m.group(0):
                n += 1
            return new
        return m.group(0)
    text = _MD_IMG_RE.sub(md_repl, text)

    def img_tag_repl(m: re.Match) -> str:
        nonlocal n
        tag = m.group(0)
        sm = _IMG_TAG_SRC_RE.search(tag)
        if sm and sm.group(2) in rel_to_url:
            new = tag.replace(sm.group(2), rel_to_url[sm.group(2)], 1)
            if new != tag:
                n += 1
            return new
        return tag
    text = _IMG_TAG_RE.sub(img_tag_repl, text)

    if text != original:
        file.write_text(text, encoding="utf-8")
    return n


# --- main pipeline ----------------------------------------------------------

def gather_refs() -> list[ImageRef]:
    all_refs: list[ImageRef] = []
    for f in iter_md_files():
        all_refs.extend(extract_refs(f))
    return all_refs


def report(refs: list[ImageRef]) -> None:
    by_file: dict[str, list[ImageRef]] = {}
    for r in refs:
        by_file.setdefault(r.repo_path, []).append(r)

    total = len(refs)
    local = [r for r in refs if r.abs_path is not None and r.abs_path.exists()]
    missing = [r for r in refs if r.abs_path is None or not r.abs_path.exists()]

    print(f"Found {total} image reference(s) across {len(by_file)} file(s).")
    print(f"  local files present: {len(local)}")
    print(f"  local files MISSING: {len(missing)}")

    if missing:
        print("\nMISSING (path resolves to nothing on disk):")
        for r in missing:
            print(f"  {r.repo_path}: {r.rel}  [{r.matched_pattern}]")

    if local:
        print("\nLOCAL (would be uploaded):")
        seen: set[str] = set()
        unique = []
        for r in local:
            if str(r.abs_path) in seen:
                continue
            seen.add(str(r.abs_path))
            unique.append(r)
        for r in unique:
            n_refs = sum(1 for x in local if x.abs_path == r.abs_path)
            print(f"  {r.rel}  ({r.abs_path.relative_to(REPO_ROOT)})  referenced by {n_refs} file(s)  [{r.matched_pattern}]")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Upload local images referenced in .md/.mdx to S3 and rewrite "
                    "the references to public URLs.",
    )
    ap.add_argument("--dry-run", "--scan", action="store_true",
                    help="Report what would change but do not upload or write any files.")
    ap.add_argument("--report", action="store_true",
                    help="Just print the scan report and exit.")
    ap.add_argument("--key-prefix",
                    default=os.environ.get("S3_KEY_PREFIX", "polza-ai-docs/assets"),
                    help="S3 key prefix (default: $S3_KEY_PREFIX or polza-ai-docs/assets).")
    ap.add_argument("--public-base", default=os.environ.get("S3_PUBLIC_BASE"),
                    help="Override the public base URL (default: <endpoint>/<bucket>).")
    args = ap.parse_args()

    refs = gather_refs()
    if not refs:
        print("No image references found.")
        return 0

    if args.report or args.dry_run:
        report(refs)
        return 0

    # Real run: need creds and S3 client.
    endpoint = os.environ.get("S3_ENDPOINT", "https://storage.yandexcloud.net")
    bucket = os.environ.get("S3_BUCKET")
    key_id = os.environ.get("S3_ACCESS_KEY_ID")
    secret = os.environ.get("S3_SECRET_ACCESS_KEY")
    region = os.environ.get("S3_REGION", "ru-central1")
    public_base = (
        args.public_base
        or os.environ.get("S3_PUBLIC_BASE")
        or (bucket and f"{endpoint}/{bucket}")
    )

    missing_creds = [n for n, v in [
        ("S3_BUCKET", bucket), ("S3_ACCESS_KEY_ID", key_id),
        ("S3_SECRET_ACCESS_KEY", secret),
    ] if not v]
    if missing_creds:
        print(
            f"ERROR: missing required env vars: {', '.join(missing_creds)}.\n"
            "Set them in your shell or a .env file (do not commit the .env).",
            file=sys.stderr,
        )
        return 2

    s3 = make_s3_client(endpoint, key_id, secret, region)
    manifest = load_manifest()
    manifest["endpoint"] = endpoint
    manifest["bucket"] = bucket
    manifest["public_base"] = public_base
    manifest["key_prefix"] = args.key_prefix

    # 1. Dedupe by abs_path and upload each unique file once.
    uploaded = 0
    skipped = 0
    failed: list[tuple[Path, str]] = []
    abs_to_url: dict[str, str] = {}

    seen_abs: set[str] = set()
    for r in refs:
        if r.abs_path is None or not r.abs_path.exists():
            continue
        key = str(r.abs_path)
        if key in seen_abs:
            continue
        seen_abs.add(key)

        data = r.abs_path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        item = manifest["items"].get(sha)
        ext = r.abs_path.suffix.lower()
        s3_key = f"{args.key_prefix.rstrip('/')}/{sha}{ext}"
        if item and item.get("s3_key") == s3_key and object_exists(s3, bucket, s3_key):
            abs_to_url[key] = item["public_url"]
            skipped += 1
            continue

        try:
            actual_sha, actual_key, ct, size = upload_image(
                s3, bucket, args.key_prefix, r.abs_path
            )
        except ClientError as e:
            failed.append((r.abs_path, str(e)))
            continue
        url = f"{public_base.rstrip('/')}/{actual_key}"
        abs_to_url[key] = url
        manifest["items"][actual_sha] = {
            "sha256": actual_sha,
            "s3_key": actual_key,
            "public_url": url,
            "source_path": str(r.abs_path.relative_to(REPO_ROOT)),
            "first_seen_in": r.repo_path,
            "content_type": ct,
            "size": size,
            "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        uploaded += 1

    # 2. Build rel -> url map and rewrite files.
    rel_to_url: dict[str, str] = {}
    for r in refs:
        if r.abs_path and str(r.abs_path) in abs_to_url:
            rel_to_url.setdefault(r.rel, abs_to_url[str(r.abs_path)])

    files_touched: set[Path] = set()
    total_subs = 0
    for f in iter_md_files():
        n = rewrite_file(f, rel_to_url)
        if n:
            files_touched.add(f)
            total_subs += n

    save_manifest(manifest)

    print(f"Uploaded: {uploaded}")
    print(f"Skipped (already in S3): {skipped}")
    print(f"Failed: {len(failed)}")
    if failed:
        for path, err in failed:
            print(f"  {path}: {err}", file=sys.stderr)
    print(f"Files rewritten: {len(files_touched)}")
    print(f"Total path substitutions: {total_subs}")
    print(f"Manifest: {MANIFEST_PATH.relative_to(REPO_ROOT)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
