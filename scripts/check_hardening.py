"""Smoke-test the bridge hardening.

Run once with the bridge up (Workspace > Scripts > CursorBridge), then once with it
stopped. Expected: WITH Origin -> 403; WITHOUT Origin -> 404 (passed the guard,
unknown route); bridge stopped -> connection errors on both.
"""
import urllib.request
import urllib.error

PORT = 9876


def call(origin=None, path="/_hardening_probe"):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path))
    if origin:
        req.add_header("Origin", origin)
    try:
        return urllib.request.urlopen(req, timeout=4).status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return "ERR: %s" % e


if __name__ == "__main__":
    print("WITH Origin header  ->", call(origin="https://evil.example"), "  (expected: 403)")
    print("WITHOUT Origin      ->", call(), "  (expected: 404 = passed guard)")
