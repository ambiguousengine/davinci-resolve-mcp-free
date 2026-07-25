"""Tiny bridge client so paths with backslashes stop dying in shell quoting."""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:9876"


def call(path, body=None):
    if body is None:
        req = urllib.request.Request(BASE + path)
    else:
        req = urllib.request.Request(
            BASE + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())


def switch(idx):
    return call("/timeline/switch", {"index": idx})


def export(fname, etype, subtype=None):
    b = {"fileName": fname, "exportType": etype}
    if subtype:
        b["exportSubtype"] = subtype
    return call("/timeline/export", b)


if __name__ == "__main__":
    print(json.dumps(call(sys.argv[1],
                          json.loads(sys.argv[2]) if len(sys.argv) > 2 else None),
                     indent=2)[:4000])
