"""The one place an SSL context is created.

The operator's office network runs a TLS-inspecting proxy, which re-signs every
connection with a corporate CA. Python's bundled OpenSSL rejects the resulting
chain outright — `certificate verify failed: Missing Authority Key Identifier`
— even though the corporate root is installed and trusted by the operating
system, because OpenSSL builds and validates the chain itself rather than
asking the OS.

`truststore` fixes exactly that by delegating verification to the platform
verifier (Schannel on Windows, Security framework on macOS, OpenSSL against the
system store on Linux). It is the project's only optional dependency and is
needed only on a network like that one; a plain VPS does not need it and the
fallback below is used instead.

**There is deliberately no way to disable verification.** Not a flag, not an
environment variable. This process reads listings that inform what the operator
pays for a watch, and a switch that silently accepts any certificate is the
kind of thing that gets turned on during a debugging session and left on.
"""

from __future__ import annotations

import ssl

_MODE = "unknown"


def ssl_context() -> ssl.SSLContext:
    """A verifying SSL context, using the OS trust store where possible."""
    global _MODE
    try:
        import truststore

        _MODE = "truststore (OS verifier)"
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        _MODE = "openssl (bundled defaults)"
        return ssl.create_default_context()


def tls_mode() -> str:
    if _MODE == "unknown":
        ssl_context()
    return _MODE


def explain_failure(exc: BaseException) -> str:
    """Turn a TLS error into the sentence that actually helps."""
    text = str(exc)
    if "Missing Authority Key Identifier" in text or "unable to get local issuer" in text:
        return (
            "TLS verification failed in a way that looks like a network doing "
            "TLS inspection. Install the optional shim with `pip install "
            "truststore` so verification uses the operating system's trust "
            "store instead of OpenSSL's bundled one. On a plain VPS you should "
            "not need it — if you see this there, the clock or the CA bundle "
            "is wrong and that is a real failure, not an inspection proxy."
        )
    return text
