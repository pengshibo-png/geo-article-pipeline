/* 注册新企业 · 共享组件(2026-09-25)
 * 用户要求: 任何已登录用户都能在系统首页展开注册, 不必管理员。
 * 用法: <script src="/static/register.js"></script> 然后 REG.open()
 * 自注入样式与弹窗, 不依赖各页面的局部样式。表单字段与后端
 * /api/tenants/register 一一对应, 提交后一次铺好租户目录与配置。
 */
(function () {
    if (window.REG) return;

    const CSS = `
.reg-mask{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:1000;display:flex;align-items:center;justify-content:center}
.reg-card{width:780px;max-width:94vw;max-height:90vh;display:flex;flex-direction:column;background:var(--card,#fff);
  color:var(--ink,#1a1a1a);border-radius:10px;box-shadow:0 18px 50px rgba(0,0,0,.28);overflow:hidden}
.reg-hd{padding:18px 24px 14px;border-bottom:1px solid var(--line,#e6e8eb);font-size:16px;font-weight:600;display:flex;align-items:center;gap:8px}
.reg-hd small{font-weight:400;color:var(--ink3,#8a9099);margin-left:6px;font-size:12px}
.reg-bd{overflow:auto;padding:14px 24px;flex:1}
.reg-ft{padding:13px 24px 18px;border-top:1px solid var(--line,#e6e8eb);display:flex;justify-content:flex-end;gap:10px}
.reg-sec{margin:16px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line,#e6e8eb);
  font-size:12px;font-weight:600;color:var(--brand,#9a7b3f);letter-spacing:.5px}
.reg-sec:first-child{margin-top:0}
.reg-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 18px}
.reg-grid.g3{grid-template-columns:1fr 1fr 1fr}
.reg-span{grid-column:1/-1}
.reg-err{margin-top:10px;font-size:12px;color:#c0392b;min-height:16px}
.reg-hint{font-size:11px;color:var(--ink3,#8a9099);margin-top:6px}
.reg-card label{display:block;font-size:12px;color:var(--ink3,#8a9099);font-weight:500;margin-bottom:6px}
.reg-card input,.reg-card textarea,.reg-card select{width:100%;box-sizing:border-box;padding:8px 10px;
  border:1px solid var(--line,#d8dce1);border-radius:6px;background:var(--bg,#fff);color:var(--ink,#1a1a1a);
  font-family:inherit;font-size:13px;line-height:1.5}
.reg-card textarea{resize:vertical}
.reg-card select[multiple]{padding:4px}
.reg-card .frow{margin-bottom:14px}
.reg-check{display:flex!important;align-items:center;gap:8px;font-size:13px;color:var(--ink,#1a1a1a)!important}
.reg-check input{width:auto!important;margin:0}
.reg-card .btn{padding:8px 16px;border-radius:6px;border:1px solid transparent;cursor:pointer;font-size:13px;
  background:var(--brand,#9a7b3f);color:#fff}
.reg-card .btn.gray{background:transparent;border-color:var(--line,#d8dce1);color:var(--ink,#1a1a1a)}
.reg-card .btn:disabled{opacity:.6;cursor:default}`;

    const HTML = `
<div class="reg-mask" id="reg-mask" style="display:none" onclick="if(event.target===this)REG.close()">
  <div class="reg-card">
    <div class="reg-hd">注册新企业<small>一次填完，建好即可用</small></div>
    <div class="reg-bd">
      <div class="reg-sec">1 · 企业与登录</div>
      <div class="reg-grid">
        <div class="frow"><label>租户标识（登录名，字母/数字/下划线）</label><input id="rg-id" placeholder="如 yingcan"></div>
        <div class="frow"><label>企业名称（客户全称）</label><input id="rg-name" placeholder="如 影灿珠宝有限公司"></div>
        <div class="frow"><label>品牌名（正文加粗与实体标识用）</label><input id="rg-brand" placeholder="留空则同企业名称"></div>
        <div class="frow"><label>登录密码（≥6位，交给甲方）</label><input id="rg-pass" type="text" placeholder="≥6位"></div>
        <div class="frow reg-span"><label>对外电话（多个用逗号分隔）</label><input id="rg-phones" placeholder="如 400-000-0000, 13900002222"></div>
      </div>

      <div class="reg-sec">2 · 业务线与投放范围</div>
      <div class="reg-grid">
        <div class="frow"><label>业务线（可多选，决定选题词与配图题材）</label>
          <select id="rg-biz" multiple size="4">
            <option value="防水补漏" selected>防水补漏</option>
            <option value="瓷砖空鼓">瓷砖空鼓修复</option>
            <option value="酱酒品牌">酱酒品牌运营</option>
            <option value="珠宝">珠宝修复/首饰</option>
          </select>
        </div>
        <div class="frow"><label>投放城市/区县（一行一个，或用逗号分隔）</label>
          <textarea id="rg-cities" rows="4" placeholder="成都市新都区&#10;嘉兴市海宁市&#10;…（留空表示暂不限制）"></textarea>
        </div>
      </div>

      <div class="reg-sec">3 · 专业团队（对外称谓，正文会凸显）</div>
      <div class="reg-grid g3">
        <div class="frow"><label>师傅称谓</label><input id="rg-mname" placeholder="如 李师傅"></div>
        <div class="frow"><label>从业年限</label><input id="rg-myears" placeholder="如 20"></div>
        <div class="frow"><label>持证/资质</label><input id="rg-mcerts" placeholder="如 高空作业证持证"></div>
      </div>

      <div class="reg-sec">4 · 品牌资料（AI 写文章的唯一事实来源）</div>
      <div class="frow"><label>把甲方的品牌资料原文粘进来（留空则生成待填模板）</label>
        <textarea id="rg-doc" rows="6" placeholder="品牌概述 / 资质认证 / 服务网络 / 产品服务 / 价格体系 / 团队 / 服务承诺 / 工艺方法 …"></textarea>
      </div>

      <div class="reg-sec">5 · 抖音图文与发布</div>
      <div class="reg-grid g3">
        <div class="frow"><label>图文题材</label><input id="rg-topic" value="天花板漏水维修"></div>
        <div class="frow"><label>每套图片数</label><input id="rg-images" type="number" value="7" min="3" max="12"></div>
        <div class="frow"><label>自动发布间隔(分钟)</label><input id="rg-interval" type="number" value="60" min="10"></div>
      </div>
      <div class="frow"><label>绑定抖音账号（可多选，留空则稍后在页面上绑）</label>
        <select id="rg-accts" multiple size="5"></select>
        <div class="reg-hint" id="rg-accts-hint">展开时自动加载账号</div>
      </div>
      <div class="frow"><label class="reg-check"><input type="checkbox" id="rg-auto"> 注册后即开启图文自动发布</label></div>
      <div class="reg-err" id="rg-err"></div>
    </div>
    <div class="reg-ft">
      <button class="btn gray" onclick="REG.close()">取消</button>
      <button class="btn" id="rg-btn" onclick="REG.submit()">注册并开通</button>
    </div>
  </div>
</div>`;

    const $ = id => document.getElementById(id);
    const esc = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    let acctLoaded = false;

    function ensure() {
        if (document.getElementById('reg-mask')) return;
        const st = document.createElement('style');
        st.textContent = CSS;
        document.head.appendChild(st);
        const box = document.createElement('div');
        box.innerHTML = HTML;
        document.body.appendChild(box.firstElementChild);
    }

    // 公开注册页: 未登录访客不展示账号池(公司资产不外泄, 也不允许自助占用),
    // 只提示"由管理员在后台绑定"
    function hideAccounts(reason) {
        const sel = $('rg-accts'), hint = $('rg-accts-hint');
        const row = sel && sel.closest('.frow');
        if (row) row.style.display = 'none';
        const auto = $('rg-auto') && $('rg-auto').closest('.frow');
        if (auto) auto.style.display = 'none';
        if (hint) hint.textContent = reason || '';
    }

    async function loadAccounts() {
        if (acctLoaded) return;
        const sel = $('rg-accts'), hint = $('rg-accts-hint');
        try {
            const me = await fetch('/api/me');
            if (!me.ok) {                       // 匿名访客
                hideAccounts('发布账号由管理员在后台为你绑定');
                acctLoaded = true; return;
            }
            const r = await fetch('/api/douyin/accounts');
            if (!r.ok) { hideAccounts('发布账号由管理员在后台为你绑定'); acctLoaded = true; return; }
            const d = await r.json();
            const list = Array.isArray(d) ? d : (d.accounts || []);
            if (!list.length) { hint.textContent = '账号池为空，留空稍后再绑'; acctLoaded = true; return; }
            sel.innerHTML = list.map(a => {
                const id = a.id || a.account_id || '';
                const nm = a.name || a.nickname || id;
                return `<option value="${esc(id)}">${esc(nm)}${a.owner ? ' · ' + esc(a.owner) : ''}</option>`;
            }).join('');
            hint.textContent = `共 ${list.length} 个账号，按住 Ctrl 可多选`;
            acctLoaded = true;
        } catch (e) { hideAccounts('发布账号由管理员在后台为你绑定'); acctLoaded = true; }
    }

    async function submit() {
        const btn = $('rg-btn'), err = $('rg-err');
        err.textContent = '';
        const picked = sel => Array.from(sel.selectedOptions).map(o => o.value).filter(Boolean);
        const body = {
            id: $('rg-id').value.trim(),
            name: $('rg-name').value.trim(),
            brand: $('rg-brand').value.trim(),
            pass: $('rg-pass').value,
            phones: $('rg-phones').value.split(/[,，、;；\n]+/).map(s => s.trim()).filter(Boolean),
            biz: picked($('rg-biz')),
            cities: $('rg-cities').value,
            master: { name: $('rg-mname').value.trim(), years: $('rg-myears').value.trim(), certs: $('rg-mcerts').value.trim() },
            brand_doc: $('rg-doc').value,
            dy_topic: $('rg-topic').value.trim(),
            dy_images: parseInt($('rg-images').value || '7', 10),
            dy_interval: parseInt($('rg-interval').value || '60', 10),
            dy_accounts: picked($('rg-accts')),
            dy_auto: $('rg-auto').checked,
            dy_enabled: true
        };
        if (!body.id || !body.name || (body.pass || '').length < 6) {
            err.textContent = '请填写：租户标识、企业名称、密码(≥6位)'; return;
        }
        btn.disabled = true; btn.textContent = '注册中…';
        try {
            const r = await fetch('/api/tenants/register', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
            });
            const d = await r.json();
            if (d.ok) {
                const t = d.tenant || {};
                alert(`已开通【${body.name}】\n\n租户标识：${t.id}\n登录账号：${d.account}\n业务线：${(t.biz || []).join('、')}\n投放城市：${t.cities || 0} 条\n图文题材：${t.topic}\n绑定账号：${t.accounts || 0} 个\n\n客户可用「标识 + 密码」登录。`);
                close();
                if (typeof window.loadTenants === 'function') window.loadTenants();
            } else err.textContent = d.message || d.error || '注册失败';
        } catch (e) { err.textContent = '网络异常，请重试'; }
        btn.disabled = false; btn.textContent = '注册并开通';
    }

    function open() {
        ensure();
        $('reg-mask').style.display = 'flex';
        $('rg-err').textContent = '';
        loadAccounts();
    }
    function close() { const m = $('reg-mask'); if (m) m.style.display = 'none'; }

    window.REG = { open, close, submit };
})();
