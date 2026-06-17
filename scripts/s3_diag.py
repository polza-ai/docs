"""S3 credential diagnostic.

Run from the repo where the script lives:
    uv run --with boto3 python3 scripts/s3_diag.py
Prints endpoint reachability, key sanity, and a no-op signed probe.
Exits 0 if everything looks good, 1 otherwise.
"""
import os
import sys
import base64
import hashlib
import hmac
import datetime
import urllib.parse
import urllib.request


def diag_basic():
    print("=== 1. Env vars ===")
    for k in ("S3_ENDPOINT", "S3_BUCKET", "S3_REGION",
              "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY",
              "S3_KEY_PREFIX", "S3_PUBLIC_BASE"):
        v = os.environ.get(k, "<unset>")
        if "SECRET" in k and v != "<unset>":
            v = v[:4] + "..." + f"({len(v)} chars)"
        print(f"  {k}: {v}")


def diag_curl_probe():
    """Hit YC with a signed HEAD on the bucket using urllib + manual sigv4.

    This isolates 'is the key valid?' from 'is boto3 misconfigured?'.
    """
    print("\n=== 2. Manual sigv4 probe (urllib, no boto3) ===")
    endpoint = os.environ.get("S3_ENDPOINT", "https://storage.yandexcloud.net")
    bucket = os.environ["S3_BUCKET"]
    region = os.environ.get("S3_REGION", "ru-central1")
    access = os.environ["S3_ACCESS_KEY_ID"]
    secret = os.environ["S3_SECRET_ACCESS_KEY"].encode()

    host = endpoint.split("//", 1)[1]
    canonical_uri = f"/{bucket}/"
    canonical_query = ""
    payload_hash = "UNSIGNED-PAYLOAD"

    t = datetime.datetime.now(datetime.timezone.utc)
    amzdate = t.strftime("%Y%m%dT%H%M%SZ")
    datestamp = t.strftime("%Y%m%d")

    canonical_headers = f"host:{host}\n" + f"x-amz-content-sha256:{payload_hash}\n" + f"x-amz-date:{amzdate}\n"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"

    canonical_request = "\n".join([
        "HEAD", canonical_uri, canonical_query,
        canonical_headers, signed_headers, payload_hash,
    ])

    algorithm = "AWS4-HMAC-SHA256"
    credential_scope = f"{datestamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join([
        algorithm, amzdate, credential_scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])

    def sign(key, msg): return hmac.new(key, msg.encode(), hashlib.sha256).digest()
    k_date = sign(("AWS4" + os.environ["S3_SECRET_ACCESS_KEY"]).encode(), datestamp)
    k_region = sign(k_date, region)
    k_service = sign(k_region, "s3")
    k_signing = sign(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

    authorization = (
        f"{algorithm} Credential={access}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    url = f"{endpoint}{canonical_uri}"
    req = urllib.request.Request(url, method="HEAD", headers={
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amzdate,
        "Authorization": authorization,
    })
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        print(f"  HTTP {resp.status}  -> key+endpoint OK")
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        print(f"  HTTP {e.code}  {e.reason}")
        print(f"  body: {body}")
        if e.code in (301, 307, 308):
            print("  -> REDIRECT. YC sometimes does this on path-style requests. Try virtual-hosted (no leading /) or set addressing_style=virtual.")
        elif e.code == 403 and "SignatureDoesNotMatch" in body:
            print("  -> SignatureDoesNotMatch. Key is BAD or signature construction is wrong.")
            print("     Verify in YC IAM that this key is active and the secret is copied without whitespace.")
        elif e.code == 403:
            print("  -> 403 not sig mismatch — likely AccessDenied. Key is OK but lacks permission.")
        elif e.code == 404:
            print("  -> 404. Auth PASSED, but bucket not found. Wrong bucket name?")
        return False
    except Exception as e:
        print(f"  ERROR: {e}")
        return False


def diag_boto3_probe():
    print("\n=== 3. boto3 HEAD probe (with addressing_style=path) ===")
    try:
        import boto3
        from botocore.client import Config
    except ImportError:
        print("  boto3 missing; skip")
        return False
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT"],
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("S3_REGION", "ru-central1"),
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    try:
        resp = s3.head_bucket(Bucket=os.environ["S3_BUCKET"])
        print(f"  HTTP 200  -> boto3 path-style OK")
        return True
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
        return False


def diag_list_buckets():
    print("\n=== 4. boto3 list_buckets ===")
    try:
        import boto3
        from botocore.client import Config
    except ImportError:
        return False
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT"],
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("S3_REGION", "ru-central1"),
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    try:
        resp = s3.list_buckets()
        names = [b["Name"] for b in resp.get("Buckets", [])]
        print(f"  HTTP 200  buckets visible: {names}")
        if os.environ["S3_BUCKET"] in names:
            print(f"  -> '{os.environ['S3_BUCKET']}' is visible. Good.")
            return True
        else:
            print(f"  -> '{os.environ['S3_BUCKET']}' is NOT in the list. Bucket doesn't exist or key lacks storage.viewer.")
            return False
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
        return False


if __name__ == "__main__":
    diag_basic()
    a = diag_curl_probe()
    b = diag_boto3_probe()
    c = diag_list_buckets()
    print("\n=== Summary ===")
    print(f"  manual-sigv4: {'OK' if a else 'FAIL'}")
    print(f"  boto3 head:   {'OK' if b else 'FAIL'}")
    print(f"  list_buckets: {'OK' if c else 'FAIL'}")
    sys.exit(0 if (a and b and c) else 1)
