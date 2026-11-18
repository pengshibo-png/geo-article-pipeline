"""config 蓝图 · 素材库（assets.html 契约的真实实现）。

页面契约（参考系统线上行为实测）：
- GET  /api/my-assets → {assets:[{id,name,url}], total}
- POST /api/my-assets （multipart，字段 files，可多文件）→ {ok, added, total, errors}
- POST /api/my-assets/delete {id} → {ok} | {ok:false, error}

文件落 data/<tenant>/assets/<uuid>.<ext>（storage.tenant_path 防穿越），
元数据入 Asset 表（note=原始文件名）；`url=/media/assets/<file>` 由本蓝图
/media 路由回吐字节。上传的图片会被写稿流水线随机取用（A 段 {image_html}）。
"""
from __future__ import annotations

import re
import secrets

from app.errors import NotFound
from app.extensions import db as _db
from app.logging import get_logger
from app.models import Asset
from app.platform import storage
from app.tenancy import TenantScopedRepository, current_tenant

log = get_logger(__name__)

_EXT_OK = {"jpg", "jpeg", "png", "webp", "gif"}
_MAX_BYTES = 5 * 1024 * 1024
_MAX_PER_POST = 50
_NAME_SANE = re.compile(r"^[\w一-鿿.\- ]{1,120}$")


def _looks_like_image(data: bytes, ext: str) -> bool:
    """扩展名可伪造，再用文件头魔数兜一道（防止把非图片塞进素材库）。"""
    if ext in ("jpg", "jpeg"):
        return data[:3] == b"\xff\xd8\xff"
    if ext == "png":
        return data[:8] == b"\x89PNG\r\n\x1a\n"
    if ext == "gif":
        return data[:6] in (b"GIF87a", b"GIF89a")
    if ext == "webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


class AssetsService:
    def __init__(self, session=None) -> None:
        self.session = session or _db.session

    def list_assets(self) -> dict:
        rows = TenantScopedRepository(Asset, self.session).query().order_by(
            Asset.id.desc()).all()
        items = [{"id": a.id, "name": a.note or a.rel_path.split("/")[-1],
                  "url": "/media/" + a.rel_path, "kind": a.kind} for a in rows]
        return {"assets": items, "total": len(items)}

    def upload(self, files) -> dict:
        repo = TenantScopedRepository(Asset, self.session)
        added, errors = 0, []
        for f in list(files)[:_MAX_PER_POST]:
            raw_name = getattr(f, "filename", "") or ""
            ext = raw_name.rsplit(".", 1)[-1].lower() if "." in raw_name else ""
            if ext not in _EXT_OK:
                errors.append(f"「{raw_name[:30]}」不是图片（支持 jpg/png/webp/gif）")
                continue
            data = f.read()
            if not data:
                errors.append(f"「{raw_name[:30]}」内容为空")
                continue
            if len(data) > _MAX_BYTES:
                errors.append(f"「{raw_name[:30]}」超过 5MB 上限")
                continue
            if not _looks_like_image(data, ext):
                errors.append(f"「{raw_name[:30]}」内容不是图片（文件头与扩展名不符）")
                continue
            fname = f"{secrets.token_hex(12)}.{ext}"
            path = storage.tenant_path("assets", fname)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            safe_name = raw_name if _NAME_SANE.match(raw_name) else f"素材.{ext}"
            repo.add(Asset(kind="image", rel_path=f"assets/{fname}", note=safe_name))
            added += 1
        self.session.commit()
        total = repo.query().count()
        if added:
            log.info("素材已上传", extra={"event": "assets_uploaded", "added": added})
        # 全部被拒时不能返回 ok:True——前端会弹"上传成功"却新增 0 张。
        if not added:
            detail = "；".join(errors[:3]) if errors else "没有收到文件"
            return {"ok": False, "error": f"没有素材入库：{detail}",
                    "added": 0, "total": total, "errors": errors}
        return {"ok": True, "added": added, "total": total, "errors": errors}

    def delete(self, asset_id) -> dict:
        repo = TenantScopedRepository(Asset, self.session)
        row = repo.query().filter(Asset.id == _int_or(asset_id, -1)).first()
        if row is None:
            raise NotFound("素材不存在")
        try:
            path = storage.tenant_path(*row.rel_path.split("/"))
            if path.exists():
                path.unlink()
        except OSError:
            log.warning("素材文件删除失败（记录仍移除）", exc_info=True)
        repo.delete(row)
        self.session.commit()
        return {"ok": True}

    def read_bytes(self, rel_path: str) -> bytes:
        """/media/assets/<file> 服务：限定 assets 目录 + 扩展名白名单。"""
        parts = (rel_path or "").split("/")
        if len(parts) != 2 or parts[0] != "assets":
            raise NotFound("非法素材路径")
        fname = parts[1]
        ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
        if ext not in _EXT_OK:
            raise NotFound("非法素材类型")
        path = storage.tenant_path("assets", fname)
        if not path.exists():
            raise NotFound("素材不存在")
        return path.read_bytes()


def _int_or(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
