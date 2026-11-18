"""蚁小二 CLI（yxer）子进程封装：信封解析、错误码归一、API Key 同步、Key 实测。

安装（skillhub 安装文档）：`npm install -g @yixiaoermail/cli`，验证 `yxer --version`。
API Key 只认 **CLI 配置文件**（`~/.yxer/config.json`，由 `yxer config set-api-key`
写入；环境变量 `YIXIAOER_API_KEY` 实测不生效，2026-10-14 联调实证）—— 所以
「API Key 随时切换」= 每次发布前把系统设置里的 Key 同步进 CLI 配置（不同才写）。

输出契约（execution-standard §5）：所有命令输出严格 JSON 信封
`{"ok": true, "data": …}` / `{"ok": false, "error": {code, category, message, …}}`，
错误码按官方分类归一为发布驱动的 PublishError.code（executor 的熔断/失效
规则按驱动码走，见 drivers.classify_error 的同族值域）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.logging import get_logger
from app.platform.fsutil import remove_tree
from app.services.publish.drivers import PublishError

log = get_logger(__name__)

# 官方错误码 → 驱动错误码（login_expired 会让 executor 标记账号待重新授权）
_CODE_MAP = {
    "YIXIAOER_AUTH_ERR": "login_expired",       # Key 缺失/失效/无权
    "YIXIAOER_ENV_ERR": "timeout",              # CLI 二进制缺失/环境问题
    "YIXIAOER_REMOTE_ERR": "upstream",          # 蚁小二后端 4xx/5xx
    "YIXIAOER_INTEGRITY_ERR": "upstream",
}
# 账号侧失败（不存在/离线）也归 login_expired：语义＝账号要重新授权
_ACCOUNT_HINTS = ("not found in target platform", "账号不存在", "不在线", "已离线",
                  "offline", "未登录")

_CONFIG_PATH: Path | None = None                # 进程内缓存（测试可重置）


def _run_subprocess(cmd, **kw):
    """子进程出口（测试替身注入点；线上就是 subprocess.run）。"""
    return subprocess.run(cmd, **kw)


def cli_exe() -> str | None:
    """yxer 可执行文件（Windows 的 npm 全局 shim 是 .CMD）。"""
    return shutil.which("yxer") or shutil.which("yxer.cmd")


def run_yxer(args: list[str], *, timeout: float = 60.0) -> dict:
    """跑一条 yxer 命令并解析 JSON 信封；失败归一为 PublishError。"""
    exe = cli_exe()
    if not exe:
        raise PublishError(
            "蚁小二 CLI 未安装：请按安装文档执行 npm install -g @yixiaoermail/cli",
            code="timeout")
    try:
        proc = _run_subprocess([exe, *args, "--json"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise PublishError(f"蚁小二 CLI 超时（{timeout:.0f}s）", code="timeout") from exc
    except OSError as exc:
        raise PublishError(f"蚁小二 CLI 启动失败：{exc}", code="timeout") from exc
    # 实测（2026-10-14 联调）：**成功信封走 stdout（rc=0），错误信封走 stderr（rc=2）**。
    # 先解 stdout，失败再解 stderr —— 只解 stdout 会把所有失败误判成"输出非 JSON"。
    out, err = (proc.stdout or "").strip(), (proc.stderr or "").strip()
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        try:
            data = json.loads(err)
        except (json.JSONDecodeError, TypeError) as exc:
            raise PublishError(f"蚁小二 CLI 输出非 JSON：{(out or err)[:120]}",
                               code="upstream") from exc
    if not isinstance(data, dict):
        raise PublishError(f"蚁小二 CLI 输出不是 JSON 对象：{str(data)[:120]}", code="upstream")
    if not data.get("ok"):
        raise _envelope_error(data)
    return data


def _envelope_error(data: dict) -> PublishError:
    err = data.get("error") or {}
    code = str(err.get("code") or "")
    message = str(err.get("message") or "蚁小二返回错误")
    hint = str(err.get("hint") or "")
    # 把 CLI 的 details（逐字段的 schema 违规原因等）带进错误文本 ——
    # 否则只看到"Schema validation failed"，不知道到底哪个字段错，只能靠猜。
    details = err.get("details")
    extra = ""
    if isinstance(details, (list, tuple)) and details:
        extra = " · " + "；".join(str(d) for d in list(details)[:6])
    elif isinstance(details, str) and details.strip():
        extra = " · " + details.strip()
    blob = f"{err.get('category') or ''} {message} {details or ''}".lower()
    if code == "YIXIAOER_AUTH_ERR" or any(h in blob for h in _ACCOUNT_HINTS):
        return PublishError(_msg(message, hint) + extra, code="login_expired")
    return PublishError(_msg(message, hint) + extra, code=_CODE_MAP.get(code, "unknown"))


def _msg(message: str, hint: str) -> str:
    return f"蚁小二：{message}" + (f"（{hint}）" if hint else "")


def cli_config_path() -> Path:
    """CLI 配置文件路径（默认 ~/.yxer/config.json；进程内缓存一次）。"""
    global _CONFIG_PATH
    if _CONFIG_PATH is None:
        _CONFIG_PATH = Path.home() / ".yxer" / "config.json"
    return _CONFIG_PATH


def sync_api_key(api_key: str) -> bool:
    """把应用的 API Key 同步进 CLI 配置（不同才写）。返回是否发生了切换。"""
    key = str(api_key or "").strip()
    if not key:
        raise PublishError("蚁小二 API Key 未配置：请在系统设置填写", code="login_expired")
    path = cli_config_path()
    try:
        current = str(json.loads(path.read_text(encoding="utf-8")).get("apiKey") or "")
    except Exception:
        current = ""                    # 读不到（未初始化/损坏）→ 无条件同步
    if current == key:
        return False
    run_yxer(["config", "set-api-key", key], timeout=20)
    log.info("yxer_api_key_switched", extra={"event": "yxer_api_key_switched"})
    return True


def probe_api_key(api_key: str) -> dict:
    """实测 Key 是否可用（临时 HOME + 一次性配置，**不碰真实 CLI 配置**）。

    用于设置页「测试连接」：CLI 只从配置文件读 Key，直接改真实配置会有副作用，
    故把带 Key 的临时配置写进临时目录、以 USERPROFILE/HOME 指向它再起子进程
     （CLI 经 os.homedir() 解析，实测遵循；配置必须无 BOM，否则 JSON 解析失败）。
    """
    key = str(api_key or "").strip()
    if not key:
        return {"ok": False, "error": "需要蚁小二 API Key"}
    exe = cli_exe()
    if not exe:
        return {"ok": False,
                "error": "蚁小二连接失败：CLI 未安装（npm install -g @yixiaoermail/cli）"}
    tmp = Path(tempfile.mkdtemp(prefix="yxer-probe-"))
    try:
        cfg_dir = tmp / ".yxer"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config.json").write_text(
            json.dumps({"apiKey": key, "localPublishClientId": ""}, ensure_ascii=False),
            encoding="utf-8")
        env = dict(os.environ, USERPROFILE=str(tmp), HOME=str(tmp))
        try:
            proc = _run_subprocess([exe, "accounts", "list", "--json"],
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=30, env=env)
            data = json.loads((proc.stdout or "").strip() or (proc.stderr or "").strip())
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "蚁小二连接失败：超时（30s）"}
        except Exception as exc:                       # noqa: BLE001 — 如实呈现
            return {"ok": False, "error": f"蚁小二连接失败：{str(exc)[:120]}"}
    finally:
        remove_tree(tmp, ignore_errors=True)
    if not isinstance(data, dict) or not data.get("ok"):
        err = (data.get("error") if isinstance(data, dict) else {}) or {}
        return {"ok": False,
                "error": f"蚁小二连接失败：{err.get('message') or '未知错误'}"}
    return {"ok": True, "msg": f"连接成功：云账号 {len(data.get('data') or [])} 个"}
