"""触发批量写稿并打印返回（不等待完成，便于后续轮询）。

用法：.venv\\Scripts\\python.exe tools\\run_batch.py <count> [--publish]
"""
from __future__ import annotations

import json
import sys
import urllib.request
import http.cookiejar

BASE = "http://127.0.0.1:7001"
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
_tok = {"v": ""}


def call(method, path, payload=None, timeout=60):
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    h = {"Content-Type": "application/json"}
    if method != "GET":
        if not _tok["v"]:
            with opener.open(BASE + "/api/csrf", timeout=10) as r:
                _tok["v"] = json.loads(r.read().decode()).get("token", "")
        h["X-CSRFToken"] = _tok["v"]
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


count = int(sys.argv[1] if len(sys.argv) > 1 else 1)
no_publish = "--publish" not in sys.argv
print("触发:", {"count": count, "no_publish": no_publish})
print("返回:", json.dumps(call("POST", "/api/run",
                              {"count": count, "no_publish": no_publish}),
                          ensure_ascii=False))
