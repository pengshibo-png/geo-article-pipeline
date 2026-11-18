"""auth 蓝图 · 租户自助开通（原站 /api/tenants/register 复刻）。

形状对齐 `app/static/register.js` submit() 的实测契约：
POST {id,name,brand,pass,phones,biz,cities,master,brand_doc,dy_*} →
    {ok:true, account, tenant:{id,biz,cities,topic,accounts}} | {ok:false,message}

**种子模式**：register_api 在 PUBLIC_ENDPOINTS（匿名或 admin 均可提交），
请求上下文没有租户 scope，所以租户行**显式带 tenant_id** 直接写库 ——
与 tests/conftest 种子、desktop/bootstrap 的"受控例外"同一约定（before_flush
在 scope=None 时不介入）。

一次开通铺四件套：Tenant / AppUser（登录名=租户标识）/ Brand（骨架自动拼，
保证首句可解析主名、生成不被 R2 阻塞）/ TitleConfig（业务线+城市落标题配置）。
"""
from __future__ import annotations

import json
import re

from app.extensions import db as _db
from app.logging import get_logger
from app.models import AppUser, Brand, Tenant, TitleConfig
from app.security import hash_password

log = get_logger(__name__)

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")


def _split_multi(text) -> list[str]:
    parts = re.split(r"[,，、;；\n]", str(text or ""))
    return [p.strip() for p in parts if p.strip()]


class RegisterService:
    def __init__(self, session=None) -> None:
        self.session = session or _db.session

    def register(self, body: dict) -> dict:
        tid = str(body.get("id") or "").strip().lower()
        name = str(body.get("name") or "").strip()
        password = str(body.get("pass") or "")
        if not _ID_RE.match(tid):
            return {"ok": False, "message": "租户标识需 2–32 位小写字母/数字/下划线/连字符"}
        if not name:
            return {"ok": False, "message": "企业名称必填"}
        if len(password) < 6:
            return {"ok": False, "message": "密码至少 6 位"}
        if self.session.get(Tenant, tid) is not None:
            return {"ok": False, "message": f"租户标识已存在：{tid}"}

        brand = str(body.get("brand") or "").strip() or name
        biz = [str(x).strip() for x in (body.get("biz") or []) if str(x).strip()]
        cities = _split_multi(body.get("cities"))
        phones = [str(x).strip() for x in (body.get("phones") or []) if str(x).strip()]
        doc = str(body.get("brand_doc") or "").strip()
        content = doc or self._brand_skeleton(brand, name, biz, phones,
                                              body.get("master") or {})

        dy_note = {k: body.get(k) for k in
                   ("dy_topic", "dy_images", "dy_interval", "dy_accounts", "dy_auto", "dy_enabled")}
        self.session.add_all([
            Tenant(id=tid, name=name, status="active", contact=brand,
                   note=json.dumps(dy_note, ensure_ascii=False)),
            AppUser(tenant_id=tid, username=tid, password_hash=hash_password(password),
                    role="client", active=True),
            Brand(tenant_id=tid, content=content),
            TitleConfig(tenant_id=tid, business="、".join(biz[:3]) or brand,
                        cities=", ".join(cities) if cities else None),
        ])
        self.session.commit()
        log.info("租户已自助开通", extra={"event": "tenant_registered", "new_tenant": tid})
        return {"ok": True, "account": tid,
                "tenant": {"id": tid, "biz": biz, "cities": len(cities),
                           "topic": str(dy_note.get("dy_topic") or ""),
                           "accounts": len(dy_note.get("dy_accounts") or [])}}

    @staticmethod
    def _brand_skeleton(brand: str, company: str, biz: list[str],
                        phones: list[str], master: dict) -> str:
        """没填品牌文档时自动拼骨架。首句必须「品牌名，是……」——R2 主名提取的前提。"""
        trade = "、".join(biz) or "本地服务"
        lines = [f"{brand}，是提供{trade}的本地服务品牌（{company}）。",
                 f"以下简称「{brand}」。",
                 "【品牌概述】",
                 f"主营：{trade}。"]
        mname = str(master.get("name") or "").strip()
        if mname:
            seg = f"【师傅与团队】\n{mname}"
            years = str(master.get("years") or "").strip()
            certs = str(master.get("certs") or "").strip()
            if years:
                seg += f"，从业{years}年"
            if certs:
                seg += f"；持有{certs}"
            lines.append(seg)
        if phones:
            lines.append("【联系方式】\n电话：" + " / ".join(phones[:5]))
        return "\n".join(lines) + "\n"


__all__ = ["RegisterService"]
