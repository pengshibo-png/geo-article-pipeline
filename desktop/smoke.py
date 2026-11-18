"""S1–S6 冒烟：对**运行中**的实例做 HTTP + 数据面校验（dev 与 dist 产物通用）。

用法：
  python -m desktop.smoke --base-url http://127.0.0.1:7001 \\
      --data-root ./data [--spec-dir ./spec] [--skip-s3]

全链路 T→A→B→C 属于 launcher --selftest（S5），这里只验证规格文件捆绑齐全。
冒烟目标是经 desktop.launcher 启动的实例（S6 依赖它挂的文件日志）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

RESULTS: list[tuple[str, bool, str]] = []

SPEC_FILES = (
    "prompts/T-标题选题.md", "prompts/A-写文章.md",
    "prompts/B-审稿质检.md", "prompts/C-修改文章.md",
    "prompts/_共享/contact-rule.md", "prompts/_共享/brand-names.md",
    "prompts/_共享/variable-registry.json",
    "prompts/_样例/validate_review.py",
    "品牌资料规范/brand-sections.json",
)


def _request(url: str, payload: dict | None = None, timeout: float = 8.0,
             headers: dict | None = None) -> tuple[int, bytes]:
    """故意不带任何 cookie —— S2 验证的正是"空 cookie 也能过"。"""
    data = None
    req_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:           # 4xx/5xx 也是有效观测
        return exc.code, exc.read()
    except urllib.error.URLError as exc:            # 连接拒绝 = 服务未起，交由判定报 FAIL
        return 0, str(exc.reason).encode("utf-8")


def _csrf_token(base: str) -> str:
    """取当前会话的 CSRF 令牌（不带 cookie 的裸取，仅用于观测）。

    CSRF 加固（app/csrf.py）落地在"冒烟 6/6"之后：非安全方法一律要带
    X-CSRFToken，否则 403。S3 是 POST，故先取令牌再登录 —— 校验的是
    "口令真实可登录"，不是"能不能绕过 CSRF"。
    """
    status, body = _request(f"{base}/api/csrf")
    if status != 200:
        return ""
    try:
        return str(json.loads(body.decode("utf-8")).get("token") or "")
    except (ValueError, AttributeError):
        return ""


def _login_with_session(base: str, payload: dict) -> tuple[int, bytes]:
    """带同一会话完成「取令牌 → 登录」。

    CSRF 令牌存在 `session["_csrf"]` 里，**必须与 cookie 同一会话**：
    裸取令牌（无 cookie）会各自生成一个匿名会话，令牌对不上照样 403。
    S2 仍用无 cookie 请求（验的正是"空 cookie 也能过"），两者互不影响。
    """
    import http.cookiejar

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(f"{base}/api/csrf", timeout=8) as resp:
        token = str(json.loads(resp.read().decode("utf-8")).get("token") or "")
    req = urllib.request.Request(f"{base}/api/auth/login",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "X-CSRFToken": token})
    try:
        with opener.open(req, timeout=8) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def check(cid: str, ok: bool, detail: str) -> None:
    RESULTS.append((cid, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} {cid}: {detail}")


def s1_healthz(base: str) -> None:
    status, body = _request(f"{base}/healthz")
    data = json.loads(body.decode("utf-8")) if status == 200 else {}
    check("S1", status == 200 and data.get("ok") is True
          and data.get("service") == "geo-article-pipeline", f"/healthz -> {status} {data}")


def s2_autologin(base: str) -> None:
    """/tenants 不在 PUBLIC_ENDPOINTS：空 cookie 拿到 200 = 自动登录生效。"""
    status, _ = _request(f"{base}/tenants")
    check("S2", status == 200, f"GET /tenants（无 cookie）-> {status}（401 = 自动登录失效）")


def s3_explicit_login(base: str, data_root: Path) -> None:
    """credentials.txt 里种下的口令真实可登录（种子正确性）。"""
    cred = data_root / "credentials.txt"
    if not cred.exists():
        check("S3", False, f"缺 {cred}（首启生成；删除后不会重建）")
        return
    password = ""
    for line in cred.read_text(encoding="utf-8").splitlines():
        if line.startswith("密码:"):
            password = line.split(":", 1)[1].strip()
    status, body = _request(f"{base}/api/auth/login",
                            payload={"username": "local", "password": password},
                            headers={"X-CSRFToken": _csrf_token(base)})
    data = json.loads(body.decode("utf-8")) if status == 200 else {}
    check("S3", status == 200 and data.get("ok") is True and data.get("role") == "admin",
          f"POST /api/auth/login -> {status} {data}")


def s4_seed(data_root: Path) -> None:
    db_file = data_root / "app.db"
    if not db_file.exists():
        check("S4", False, f"缺数据库 {db_file}")
        return
    con = sqlite3.connect(str(db_file))
    try:
        tenants = con.execute("SELECT COUNT(*) FROM tenant WHERE id='local'").fetchone()[0]
        users = con.execute("SELECT COUNT(*) FROM app_user WHERE tenant_id='local'").fetchone()[0]
        brands = con.execute("SELECT COUNT(*) FROM brand WHERE tenant_id='local'").fetchone()[0]
        configs = con.execute(
            "SELECT COUNT(*) FROM title_config WHERE tenant_id='local'").fetchone()[0]
    finally:
        con.close()
    ok = (tenants, users, brands, configs) == (1, 1, 1, 1)
    check("S4", ok, f"种子 tenant={tenants} user={users} brand={brands} title_config={configs}")


def s5_spec_files(spec_dir: Path | None) -> None:
    if spec_dir is None:
        check("S5", True, "跳过（未给 --spec-dir；全链路见 launcher --selftest）")
        return
    missing = [rel for rel in SPEC_FILES if not (spec_dir / rel).is_file()]
    check("S5", not missing,
          f"规格捆绑 9 件：{'齐' if not missing else f'缺 {missing}'}")


def s6_log(data_root: Path) -> None:
    log_file = data_root / "logs" / "app.log"
    if not log_file.exists():
        check("S6", False, f"缺日志 {log_file}（须经 desktop.launcher 启动）")
        return
    text = log_file.read_text(encoding="utf-8", errors="replace")
    check("S6", '"request_id"' in text, f"{log_file} 含结构化 request_id 行")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # GBK 控制台保险
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="GEO 桌面版冒烟自检")
    parser.add_argument("--base-url", default="http://127.0.0.1:7001")
    parser.add_argument("--data-root", default=None, help="服务实例的 data 根")
    parser.add_argument("--spec-dir", default=None, help="geo_spec 目录（dist 传 _internal/geo_spec）")
    parser.add_argument("--skip-s3", action="store_true", help="跳过显式登录检查")
    args = parser.parse_args()

    from desktop.paths import data_dir

    data_root = Path(args.data_root) if args.data_root else data_dir()
    base = args.base_url.rstrip("/")
    s1_healthz(base)
    s2_autologin(base)
    if not args.skip_s3:
        s3_explicit_login(base, data_root)
    s4_seed(data_root)
    s5_spec_files(Path(args.spec_dir) if args.spec_dir else None)
    s6_log(data_root)

    failed = [cid for cid, ok, _ in RESULTS if not ok]
    print(f"\n冒烟结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过"
          + (f"；失败：{failed}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
