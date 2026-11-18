"""SigV4 query authentication (sizeup/aws.py presign): an S3 presigned URL signs "UNSIGNED-PAYLOAD" as the payload
hash (S3 refuses anything else with SignatureDoesNotMatch, which broke the intel Listen links), every other service
the empty body's sha256 (Transcribe's streaming URL)."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urlsplit

from sizeup import aws

EXAMPLE = aws.Credentials("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
AT = datetime(2013, 5, 24, tzinfo=timezone.utc)


def sig(url: str) -> str:
    return parse_qs(urlsplit(url).query)["X-Amz-Signature"][0]


def test_s3_presigned_get_matches_the_aws_example_by_default():
    # AWS S3 docs, "Authenticating Requests: Using Query Parameters", Example: GET object, presigned URL.
    u = aws.presign("https://examplebucket.s3.amazonaws.com/test.txt", EXAMPLE, "us-east-1", "s3", 86400, AT)
    assert u == ("https://examplebucket.s3.amazonaws.com/test.txt?X-Amz-Algorithm=AWS4-HMAC-SHA256"
                 "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request"
                 "&X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host"
                 "&X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")


def _by_hand(host: str, path: str, pairs: list[tuple[str, str]], payload: str, creds: aws.Credentials, reg: str,
             service: str, amz: str, day: str) -> str:
    """The signature built straight from the SigV4 spec, not through sizeup/aws.py."""
    q = "&".join(f"{k}={v}" for k, v in sorted((quote(k, safe="-_.~"), quote(v, safe="-_.~")) for k, v in pairs))
    creq = "\n".join(["GET", path, q, f"host:{host}\n", "host", payload])
    scope = f"{day}/{reg}/{service}/aws4_request"
    sts = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(creq.encode()).hexdigest()])
    k = ("AWS4" + creds.secret_key).encode()
    for part in (day, reg, service, "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    return hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()


def test_s3_presign_with_response_overrides_signs_unsigned_payload():
    creds = aws.Credentials("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "example-session-token")
    host = "examplebucket.s3.us-east-2.amazonaws.com"
    u = aws.presign(f"https://{host}/listen/abc.wav?response-content-disposition=inline"
                    "&response-content-type=audio%2Fwav", creds, "us-east-2", "s3", 1200, AT)
    pairs = [("response-content-disposition", "inline"), ("response-content-type", "audio/wav"),
             ("X-Amz-Algorithm", "AWS4-HMAC-SHA256"),
             ("X-Amz-Credential", "AKIAIOSFODNN7EXAMPLE/20130524/us-east-2/s3/aws4_request"),
             ("X-Amz-Date", "20130524T000000Z"), ("X-Amz-Expires", "1200"), ("X-Amz-SignedHeaders", "host"),
             ("X-Amz-Security-Token", "example-session-token")]
    args = (host, "/listen/abc.wav", pairs)
    rest = (creds, "us-east-2", "s3", "20130524T000000Z", "20130524")
    assert sig(u) == _by_hand(*args, "UNSIGNED-PAYLOAD", *rest)
    assert sig(u) != _by_hand(*args, aws.EMPTY_SHA256, *rest)
    q = parse_qs(urlsplit(u).query)
    assert q["response-content-disposition"] == ["inline"] and q["response-content-type"] == ["audio/wav"]


def test_the_listen_link_is_signed_with_unsigned_payload():
    from sizeup.intel import listen as L
    creds = aws.Credentials("AKIDEXAMPLE", "secret", "token")
    st = L.S3ListenStore("audio-bucket-example", "us-east-1", send=lambda *a: (200, {}, b""), creds=lambda: creds)
    u = st.presign("listen/abc.wav", 20 * 60, "audio/wav")
    q = parse_qs(urlsplit(u).query)
    at = datetime.strptime(q["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    base =st.s3.url("listen/abc.wav") + "?response-content-disposition=inline&response-content-type=audio%2Fwav"
    assert sig(u) == sig(aws.presign(base, creds, "us-east-1", "s3", 1200, at, aws.UNSIGNED_PAYLOAD))
    assert sig(u) != sig(aws.presign(base, creds, "us-east-1", "s3", 1200, at, aws.EMPTY_SHA256))


def test_other_services_still_sign_the_empty_payload_hash():
    url = "wss://transcribestreaming.us-east-2.amazonaws.com:8443/stream-transcription-websocket?language-code=en-US"
    u = aws.presign(url, EXAMPLE, "us-east-2", "transcribe", 300, AT)
    assert sig(u) == sig(aws.presign(url, EXAMPLE, "us-east-2", "transcribe", 300, AT, aws.EMPTY_SHA256))
    assert sig(u) != sig(aws.presign(url, EXAMPLE, "us-east-2", "transcribe", 300, AT, aws.UNSIGNED_PAYLOAD))
