// ============ 共享布局 · 多租户商业版 ============
// 全局fetch包装: 401自动跳登录 + CSRF令牌自动携带
(function () {
    const _f = window.fetch;
    let redirecting = false;

    // CSRF: 一次性取令牌缓存; 同源的非安全方法自动带 X-CSRFToken 头。
    // 令牌在服务端按会话签发(见 app/csrf.py), 服务端开启校验时才需要。
    const _csrfReady = (async () => {
        try {
            const r = await _f('/api/csrf', { headers: { Accept: 'application/json' } });
            const d = await r.json();
            _csrfToken = (d && d.token) || '';
        } catch (e) { _csrfToken = ''; }
        return _csrfToken;
    })();
    let _csrfToken = '';

    function _urlOf(input) {
        if (typeof input === 'string') return input;
        return (input && input.url) || '';
    }

    function _methodOf(input, init) {
        const m = (init && init.method) || (typeof input !== 'string' && input && input.method) || 'GET';
        return String(m).toUpperCase();
    }

    window.fetch = function (input, init) {
        const mutating = ['POST', 'PUT', 'PATCH', 'DELETE'].includes(_methodOf(input, init))
            && /^\//.test(_urlOf(input));

        const send = (tok) => {
            if (mutating) {
                init = init || {};
                const h = new Headers(init.headers || {});
                if (tok) h.set('X-CSRFToken', tok);
                init.headers = h;
            }
            return _f.call(this, input, init).then(r => {
                // 401只跳一次且不抛错(抛错会中断页面全部JS, 造成"卡死"观感)
                if (r.status === 401 && !redirecting) {
                    redirecting = true;
                    setTimeout(() => location.href = '/login', 300);
                }
                return r;
            });
        };
        if (!mutating) return send('');            // 安全方法: 原样走(保留401处理)
        return _csrfToken ? send(_csrfToken) : _csrfReady.then(send);
    };
})();

const NAV_GROUPS = [
    {
        title: '内容生产', items: [
            { id: 'dash', url: '/', ic: 'gauge', icon: '◈', name: '仪表盘', hero: ['总览驾驶舱', '生成流水线状态 · 标题库进度 · 实时日志'] },
            { id: 'titles', url: '/titles', ic: 'list', icon: '☰', name: '关键词管理', hero: ['关键词库', '批量管理文章选题 · 一行一个 · 支持追加覆盖'] },
            { id: 'gen', url: '/gen', ic: 'pencil', icon: '✦', name: '文章生成', hero: ['批量生成引擎', 'AI 并发写作 · 审稿修改 · 实时日志'] },
            { id: 'douyin', url: '/douyin', ic: 'image', icon: '♪', name: '抖音图文', hero: ['抖音图文产线', 'AI文案 + AI生图 + 话题标签 · 定时生成'] },
            { id: 'video', url: '/video', ic: 'video', icon: '▶', name: '抖音视频', hero: ['AI短视频产线', '上门实拍纪录 · AI分镜+配音+字幕 · 定时生成发布'] },
            { id: 'brand', url: '/brand', ic: 'building', icon: '⌂', name: '品牌资料', hero: ['品牌资料库', '我的品牌信息 · 全渠道文章共用'] },
            { id: 'assets', url: '/assets', ic: 'layers', icon: '▣', name: '素材库', hero: ['图片素材库', '上传配图素材 · 文章自动随机取用居中插入'] },
            { id: 'audience', url: '/audience', ic: 'users', icon: '◎', name: '客户画像', hero: ['客户画像', '目标人群输入 · AI洞察 · 精准写作'] },
            { id: 'cities', url: '/cities', ic: 'pin', icon: '⊞', name: '业务城市', hero: ['业务城市', '城市选择 · 选题带城市 · 发布对应区域'] },
            { id: 'prompts', url: '/prompts', ic: 'code', icon: '✎', name: 'AI指令', hero: ['AI指令自定义', '标题选题 · 写作 · 审稿 · 修改 四段指令自定义'] },
        ]
    },
    {
        title: '发布分发', items: [
            { id: 'tasks', url: '/tasks', ic: 'send', icon: '➤', name: '任务中心', hero: ['发布任务中心', '文章分配账号 · 自动排队执行 · 搜狐/美篇/列表网/58同城'] },
            { id: 'archives', url: '/archives', ic: 'archive', icon: '▤', name: '文章档案', hero: ['文章档案库', '初稿 · 审稿 · 终稿 · 多平台发布状态全记录'] },
            { id: 'publog', url: '/publog', ic: 'log', icon: '≡', name: '发布日志', hero: ['发布日志', '全渠道发布记录 · 成功失败 · 文章链接'] },
        ]
    },
    {
        title: '数据洞察', items: [
            { id: 'geo', url: '/geo', ic: 'radar', icon: '▲', name: 'GEO监测', hero: ['GEO 效果监测', 'AI搜索品牌提及率 · 引用归因 · 四引擎矩阵分析'] },
            { id: 'citations', url: '/citations', ic: 'link', icon: '◉', name: '引用来源', hero: ['引用来源', 'AI 回答引用的内容来源与频次'] },
            { id: 'reports', url: '/reports', ic: 'download', icon: '▤', name: '报告中心', hero: ['报告中心', '数据导出与定期报告'] },
            { id: 'competitor', url: '/competitor', ic: 'target', icon: '◎', name: '竞品对比', hero: ['竞品对比', '与同行品牌的可见度对比'] },
        ]
    },
    {
        title: '系统', items: [
            { id: 'register', url: '/register', ic: 'plus', icon: '✚', name: '注册新企业', hero: ['注册新企业', '填一次即建好租户 · 配置 · 品牌资料 · 图文与发布账号'] },
            { id: 'tenants', url: '/tenants', ic: 'city', icon: '⬢', name: '客户管理', admin: true, hero: ['客户管理', '多客户租户 · 客户账号 · 一键开通'] },
            { id: 'auth', url: '/auth', ic: 'shield', icon: '◉', name: '账号授权', hero: ['账号授权', '添加自媒体账号 · 多号管理 · 登录验证'] },
            { id: 'settings', url: '/settings', ic: 'sliders', icon: '❖', name: '系统设置', admin: true, hero: ['系统设置', '模型参数 · 流水线步骤开关'] },
        ]
    },
];

let ME = null; // 当前用户信息(/api/me)

function navVisible() {
    const groups = NAV_GROUPS.map(g => ({ ...g, items: g.items.filter(n => !n.admin || (ME && ME.role === 'admin')) }))
        .filter(g => g.items.length);
    return groups;
}

async function loadMe() {
    try {
        const r = await fetch('/api/me');
        if (!r.ok) return;
        ME = await r.json();
    } catch (e) { }
    renderSidebar();
}

async function switchTenant(tid) {
    await fetch('/api/admin/view-tenant', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tenant_id: tid, reason: '顶栏切换租户视角' }) });
    location.reload();
}


// 发布失败原因 → 面向甲方的人话表述(各页可复用)
function friendlyErr(e) {
    const t = String(e || '');
    if (!t) return '';
    if (/熔断|限流|未生效过多|风控/.test(t)) return '渠道限流：系统已自动暂停该渠道并稍后重试';
    if (/登录|未登录|cookie|Cookie|失效|过期/.test(t)) return '账号登录已失效：请到「账号授权」重新登录';
    if (/额度|上限|次数/.test(t)) return '今日发布额度已用完：系统将顺延到下一时段';
    if (/超时|timeout|连接/.test(t)) return '网络超时：系统会自动重试';
    if (/验证码|captcha/.test(t)) return '触发平台验证码：需人工处理后继续';
    return t.slice(0, 40);
}

async function doLogout() {
    await fetch('/api/auth/logout', { method: 'POST' });
    location.href = '/login';
}

// 侧栏内容(客户版 .cl-side 与管理版 .sidebar 共用同一套结构, 避免三种侧栏样式并存)
function sidebarInner(cur, isAdmin) {
    const groups = navVisible();
    const nav = groups.map(g =>
        `<div class="cl-ngt">${g.title}</div>` +
        g.items.map(n => {
            const nm = (n.id === 'dash' && !isAdmin) ? '效果报告' : n.name;
            return `<a href="${n.url}" class="cl-na ${n.id === cur ? 'on' : ''}"><span class="ic" data-ic="${n.ic || 'grid'}"></span>${nm}</a>`;
        }).join('')
    ).join('');
    let tenantSel = '';
    // 顶栏已有租户切换时, 侧栏不再重复渲染
    if (isAdmin && ME && ME.tenants && ME.tenants.length && !document.getElementById('tb-tenant')) {
        const opts = ME.tenants.map(t => `<option value="${t.id}" ${t.id === ME.tenant ? 'selected' : ''}>${esc(t.name)}</option>`).join('');
        tenantSel = `<div class="cl-ngt" style="padding-top:14px">当前客户</div>
            <select class="cl-tsel" onchange="if(this.value)switchTenant(this.value)">${opts}</select>`;
    }
    const who = isAdmin ? ((ME && ME.name) || '管理员') : ((ME && (ME.tenant_name || ME.name)) || '客户');
    const initial = String(who).slice(0, 2);
    return `<div class="cl-brand">
            <div class="cl-logo">G</div>
            <div><div class="bt">GEO优化系统</div><div class="bs">${isAdmin ? '运营管理端' : '客户工作台'}</div></div>
        </div>
        <div class="cl-user">
            <div class="av">${esc(initial)}</div>
            <div class="nm">${esc(who)}</div>
            <span class="out" onclick="doLogout()">退出</span>
        </div>
        ${tenantSel}
        <nav class="cl-nav">${nav}</nav>`;
}

function renderSidebar() {
    const cur = document.body.dataset.page;
    // 侧栏样式跟随"页面样式"(cl-* 客户版页 / 深色管理版页), 而不是用户角色——
    // 管理员打开客户版页面(任务中心/GEO等)时必须同样渲染客户版侧栏, 否则标记无任何样式(裸链接)
    const isClientStylePage = !!document.querySelector('.cl-side');

    // ===== 客户版页面: 新设计系统侧栏(管理员同构渲染, 附租户切换) =====
    // ME 未取到时(接口失败)落到管理版分支兜底, 避免读 null 抛错中断页面初始化
    if (ME && (isClientStylePage || ME.role === 'client')) {
        const isAdmin = ME && ME.role === 'admin';
        // 管理端外壳统一: 管理员在客户版页面上也使用深色侧栏(样式见 ds.css .shell-admin)
        if (isAdmin && isClientStylePage) document.body.classList.add('shell-admin');
        const sb = document.getElementById('sidebar');
        if (!sb) return;
        sb.innerHTML = sidebarInner(cur, isAdmin);
        if (window.ICONS) ICONS.mount(sb);
        return;
    }

    // ===== 管理版页面: 同一套侧栏内容, 容器是 .sidebar =====
    const sb = document.getElementById('sidebar');
    if (sb) { sb.innerHTML = sidebarInner(cur, !!(ME && ME.role === 'admin')); if (window.ICONS) ICONS.mount(sb); }
}

// 顶栏(浅色企业版页面): 页内静态提供骨架, 这里只填用户/租户/环境名
function renderTopbar() {
    const tb = document.getElementById('topbar');
    if (!tb) return;
    const isAdmin = !!(ME && ME.role === 'admin');
    const who = isAdmin ? ((ME && ME.name) || '管理员') : ((ME && (ME.tenant_name || ME.name)) || '客户');
    const uname = document.getElementById('tb-uname');
    if (uname) uname.textContent = who;
    const av = document.getElementById('tb-av');
    if (av) av.textContent = String(who).slice(0, 1);
    const env = document.getElementById('tb-env');
    if (env) env.textContent = isAdmin ? '运营管理端' : '客户工作台';
    const tsel = document.getElementById('tb-tenant');
    if (tsel) {
        if (isAdmin && ME.tenants && ME.tenants.length) {
            tsel.innerHTML = ME.tenants.map(t => `<option value="${t.id}" ${t.id === ME.tenant ? 'selected' : ''}>${esc(t.name)}</option>`).join('');
            tsel.onchange = () => { if (tsel.value) switchTenant(tsel.value); };
        } else {
            tsel.style.display = 'none';
            const chip = tsel.closest('.tb-chip'); if (chip) chip.style.display = 'none';
        }
    }
    const um = document.getElementById('tb-menu');
    const ub = document.getElementById('tb-user-btn');
    if (um && ub && !ub.dataset.wired) {
        ub.dataset.wired = '1';
        ub.onclick = e => { e.stopPropagation(); um.classList.toggle('on'); };
        document.addEventListener('click', () => um.classList.remove('on'));
    }
}

// 面包屑: 模块 / 页面, 由 nav 配置自动派生(24 个视图全站一致)
function renderCrumb() {
    const el = document.getElementById('crumb');
    if (!el) return;
    const cur = document.body.dataset.page;
    const map = {};
    NAV_GROUPS.forEach(g => g.items.forEach(n => { map[n.id] = { g: g.title, n: n.name }; }));
    const hit = map[cur];
    const isClient = !!(ME && ME.role === 'client');
    const home = '首页';
    if (hit) {
        const label = (hit.n === '仪表盘' && isClient) ? '效果报告' : hit.n;
        el.innerHTML = `<a href="/">${home}</a><i>/</i><span>${hit.g}</span><i>/</i><span class="on">${label}</span>`;
    } else {
        const t = (document.title || '').split(/[·\-|]/)[0].trim() || '页面';
        el.innerHTML = `<a href="/">${home}</a><i>/</i><span class="on">${t}</span>`;
    }
}

function renderHero() {
    if (ME && ME.role === 'client') return; // 甲方端每页自带cl-head页头
    // 页面已有自己的页头组件(统一页头 phead / 驾驶舱 cmd / GEO geo-cmd)时不再注入, 避免重复标题
    if (document.querySelector('.phead, .cmd, .geo-cmd')) return;
    const cur = document.body.dataset.page;
    const item = NAV_GROUPS.flatMap(g => g.items).find(n => n.id === cur);
    if (!item) return;
    const heroTxt = (cur === 'dash' && ME && ME.role === 'client')
        ? ['GEO 效果总览', 'AI 搜索表现 · 内容资产 · 发布动态 一站式查看']
        : item.hero;
    if (!heroTxt) return;
    const main = document.querySelector('.main');
    if (!main || main.querySelector('.page-hero')) return;
    const hero = document.createElement('div');
    hero.className = 'page-hero';
    hero.innerHTML = `<div class="ph-title">${heroTxt[0]}</div><div class="ph-desc">${heroTxt[1]}</div>`;
    main.insertBefore(hero, main.firstChild);
}

function esc(s) {
    return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function platformName(p) {
    const m = { meipian: '美篇', lieju: '列表网', wuba: '58同城', souhu: '搜狐', baijiahao: '百家号', other: '其他' };
    return m[p] || p || '搜狐';
}
async function refreshHeader() {
    try {
        const r = await fetch('/api/status');
        const d = await r.json();
        const tag = document.getElementById('run-tag');
        if (tag) {
            // 颜色走 token(浅色/深色主题各自成立), 不在 JS 里硬编码色值
            if (d.running) { tag.textContent = '生成运行中'; tag.style.color = 'var(--warn)'; tag.classList.add('run'); }
            else { tag.textContent = '空闲'; tag.style.color = 'var(--ink2)'; tag.classList.remove('run'); }
        }
        const mt = document.getElementById('model-tag');
        try {
            const r2 = await fetch('/api/config');
            if (r2.ok) {
                const c = await r2.json();
                if (mt) mt.textContent = '模型 · ' + c.ai.model;
            } else if (mt) mt.textContent = '模型 · —';
        } catch (e) { if (mt) mt.textContent = '模型 · —'; }
    } catch (e) { }
}

// 页面初始化入口(各页onload调用layoutInit)
function layoutInit() {
    // 拆分多页后每页panel需激活(旧单页版靠showPanel切换, 拆页后默认激活)
    document.querySelectorAll('.panel').forEach(p => p.classList.add('active'));
    loadMe().then(() => {
        renderHero();
        renderTopbar();
        renderCrumb();
        refreshHeader();
    });
}

// 自动初始化(所有业务页通用, 各页自身的loadXXX由自己onload调用)
document.addEventListener('DOMContentLoaded', () => { layoutInit(); });

// 兼容旧页面调用: loadConfig = 顶栏模型/状态标签刷新
function loadConfig() { refreshHeader(); }
