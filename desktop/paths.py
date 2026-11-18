"""桌面形态的路径唯一出口：dev 直跑与 PyInstaller 冻结态共用。

pin_environment() 必须在**任何 app.* import 之前**调用 —— 规格文件全部惰性读取
（spec.py 只在请求期打开文件），因此设好环境变量的时机即充分。

钉值用 setdefault 语义：用户真实的进程环境变量优先；exe 旁 / 项目根的 .env 仍由
pydantic-settings 正常加载（有 .env 时桌面启动器不再强推 fake 供应商）。
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path


def is_frozen() -> bool:
    """是否运行在 PyInstaller 产物里。"""
    return getattr(sys, "frozen", False)


def exe_dir() -> Path:
    """exe 所在目录；dev 态 = 项目根（desktop/ 的上一级）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundle_dir() -> Path:
    """只读资源根（PyInstaller 6 onedir 的 _internal；dev 态即项目根）。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return exe_dir()


def bundled_spec_dir() -> Path:
    """构建时捆进 _internal/geo_spec 的 S3 冻结规格。"""
    return bundle_dir() / "geo_spec"


def data_dir() -> Path:
    """运行期数据根（portable：exe 旁，用户可见可备份可整删）。"""
    return exe_dir() / "data"


def logs_dir() -> Path:
    return data_dir() / "logs"


def pin_environment() -> None:
    """锚定 CWD 并钉死桌面形态所需的全部环境变量。"""
    root = exe_dir()
    os.chdir(root)                                   # .env / 相对路径统一锚定
    os.environ.setdefault("GEO_DESKTOP", "1")        # autologin 的生效开关
    data = data_dir()
    os.environ.setdefault("GEO_DATA_ROOT", data.as_posix())
    # Flask-SQLAlchemy 3 会把**相对** sqlite 路径重定位到 instance/（dev 实测：
    # ./data/app.db → instance/data/app.db，父目录缺失即 unable to open）。
    # 桌面形态要求数据固定在 exe 旁 —— dev 与冻结态一律显式给绝对路径。
    os.environ.setdefault("GEO_DATABASE_URL", "sqlite:///" + (data / "app.db").as_posix())
    if not is_frozen():
        return                                       # dev 态：尊重项目 .env 与默认值

    # 桌面形态规格只有 bundle 一份，覆盖优先于 setdefault（环境变量残留会指错地方）。
    os.environ["GEO_SPEC_DIR"] = bundled_spec_dir().as_posix()
    if not (root / ".env").exists():
        # 开箱离线可跑；用户建了 .env 则以 .env 为准（可配真实 LLM Key）。
        os.environ.setdefault("GEO_LLM_PROVIDER", "fake")
    _pin_secret(data / "secret_key.txt")


def _pin_secret(secret_file: Path) -> None:
    """持久化会话密钥：重启后浏览器会话仍有效。真实环境变量优先。"""
    if "GEO_SECRET_KEY" in os.environ:
        return
    if secret_file.exists():
        key = secret_file.read_text(encoding="utf-8").strip()
    else:
        key = secrets.token_urlsafe(32)
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(key, encoding="utf-8")
    if key:
        os.environ["GEO_SECRET_KEY"] = key
