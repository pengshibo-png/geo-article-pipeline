"""Cookie 归一化（browser.py 拆分，A3 行数预算）。

用户从浏览器里复制出来的 Cookie 有三种常见形态，都要能吃：
  1. 请求头串   `a=1; b=2`
  2. JSON 数组  EditThisCookie / 插件导出（[{name,value,domain,path}]）
  3. Netscape   cookies.txt（7 列制表符分隔，首行常是注释）
统一成 Playwright `context.add_cookies()` 要的 [{name,value,domain,path}]。
"""
from __future__ import annotations

import json
import re

_SUFFIX2 = ("com", "net", "org", "gov", "edu")   # 二级后缀（.com.cn 这类）


def cookie_domain(url: str) -> str:
    """由站点 URL 推 Cookie 域：`mp.sohu.com` → `.sohu.com`。"""
    host = re.sub(r"^https?://", "", url or "").split("/")[0].split(":")[0]
    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:
        return host
    if len(parts[-1]) == 2 and parts[-2] in _SUFFIX2:
        return "." + ".".join(parts[-3:])
    return "." + ".".join(parts[-2:])


def parse_cookies(raw: str, *, domain: str) -> list[dict]:
    """把粘贴的 Cookie 归一成 Playwright 的 cookie 列表（识别不了 → 空列表）。"""
    text = (raw or "").strip()
    if not text:
        return []
    if text[0] in "[{":
        return _from_json(text, domain)
    if any(len(line.split("\t")) >= 7 for line in text.splitlines()):
        return _from_netscape(text, domain)          # cookies.txt（首行常是注释）
    out = []
    for pair in re.split(r"[;\n]", text):
        if "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        name, value = name.strip(), value.strip()
        if name:
            out.append({"name": name, "value": value, "domain": domain, "path": "/"})
    return out


def account_cookies(account, *, domain: str) -> list[dict]:
    """解密账号 Cookie（Fernet 密文在 Account.cookie_blob）并归一。"""
    from app.services.crypto import decrypt_text

    return parse_cookies(decrypt_text(account.cookie_blob), domain=domain)


def _from_json(text: str, domain: str) -> list[dict]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if isinstance(data, dict):
        rows = data.get("cookies")
    else:
        rows = data
    out = []
    for c in rows or []:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        dom = str(c.get("domain") or domain)
        out.append({"name": str(c["name"]), "value": str(c.get("value") or ""),
                    "domain": dom if "." in dom else domain,
                    "path": str(c.get("path") or "/")})
    return out


def _from_netscape(text: str, domain: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) < 7:
            continue
        dom = cols[0].strip() or domain
        out.append({"name": cols[5].strip(), "value": cols[6].strip(),
                    "domain": dom if "." in dom else domain, "path": cols[2].strip() or "/"})
    return [c for c in out if c["name"]]


__all__ = ["account_cookies", "cookie_domain", "parse_cookies"]
