/* ============================================================
   icons.js · 内联 SVG 线性图标集 (零依赖)
   用法一(静态):  <i class="ic" data-ic="archive" data-sz="16"></i>
   用法二(动态):  元素 innerHTML 里带 data-ic 时, 渲染后调用 ICONS.mount(容器)
   约定: 16/20px, stroke=currentColor, 1.5 线宽, 无填充块
   ============================================================ */
(function () {
    const P = {
        // —— 导航/模块 ——
        gauge: '<path d="M12 13.5 15.5 10"/><path d="M3.5 17a9 9 0 1 1 17 0"/><circle cx="12" cy="17" r="1.4"/>',
        list: '<path d="M4 6h2M4 12h2M4 18h2M9 6h11M9 12h11M9 18h11"/>',
        pencil: '<path d="M4 20h4L19.5 8.5a2.1 2.1 0 0 0-3-3L5 17v3Z"/><path d="M15 6.5l2.5 2.5"/>',
        image: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="M4 17l4.5-4 4 3.5 3-2.5 4 3.5"/>',
        video: '<rect x="3.5" y="5.5" width="17" height="13" rx="2"/><path d="M10.5 9.5l4 2.5-4 2.5z"/>',
        film: '<rect x="3.5" y="5" width="17" height="14" rx="2"/><path d="M8 5v14M16 5v14M3.5 12h17"/>',
        building: '<path d="M5 20V5.5A1.5 1.5 0 0 1 6.5 4h7A1.5 1.5 0 0 1 15 5.5V20"/><path d="M15 10h3.5A1.5 1.5 0 0 1 20 11.5V20"/><path d="M3 20h18"/><path d="M8 8h4M8 12h4M8 16h4"/>',
        layers: '<path d="M12 4l8 4-8 4-8-4 8-4Z"/><path d="M4 12l8 4 8-4"/><path d="M4 16l8 4 8-4"/>',
        users: '<circle cx="9" cy="9" r="3"/><path d="M3.5 19a5.5 5.5 0 0 1 11 0"/><path d="M16 7.2a3 3 0 0 1 0 5.6M17 19a5.5 5.5 0 0 0-2-4.2"/>',
        pin: '<path d="M12 21s6-5.3 6-10a6 6 0 1 0-12 0c0 4.7 6 10 6 10Z"/><circle cx="12" cy="11" r="2.2"/>',
        code: '<path d="M9 7l-5 5 5 5M15 7l5 5-5 5"/>',
        send: '<path d="M20 4 3.5 10.5l6.5 2.5 2.5 6.5L20 4Z"/><path d="M10 13l3.5-3.5"/>',
        archive: '<rect x="3.5" y="4.5" width="17" height="4.5" rx="1.4"/><path d="M5.5 9v9.5A1.5 1.5 0 0 0 7 20h10a1.5 1.5 0 0 0 1.5-1.5V9"/><path d="M10 13h4"/>',
        log: '<path d="M4 5.5h16M4 12h16M4 18.5h10"/>',
        radar: '<path d="M12 12 18 7"/><path d="M20.5 12a8.5 8.5 0 1 1-8.5-8.5"/><path d="M12 3.5V12l6 5"/>',
        shield: '<path d="M12 3.5 19 6v6c0 4.2-3 7-7 8.5C8 19 5 16.2 5 12V6l7-2.5Z"/><path d="M9.3 12.2l2 2 3.4-3.6"/>',
        sliders: '<path d="M4 7h9M17 7h3M4 17h4M12 17h8"/><circle cx="15" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>',
        grid: '<rect x="4" y="4" width="7" height="7" rx="1.4"/><rect x="13" y="4" width="7" height="7" rx="1.4"/><rect x="4" y="13" width="7" height="7" rx="1.4"/><rect x="13" y="13" width="7" height="7" rx="1.4"/>',
        // —— 数据/图表 ——
        chartLine: '<path d="M4 19V4.5"/><path d="M4 19h16"/><path d="M7 15l3.5-4.5 3 2.5L19 7"/>',
        chartBar: '<path d="M4 19V4.5"/><path d="M4 19h16"/><path d="M8 16v-4M12 16V9M16 16v-6"/>',
        chartPie: '<path d="M12 4.5A7.5 7.5 0 1 0 19.5 12H12V4.5Z"/><path d="M14.5 3.6A7.5 7.5 0 0 1 20.4 9.5H14.5V3.6Z"/>',
        heat: '<rect x="4" y="4" width="4.5" height="4.5" rx="1"/><rect x="9.8" y="4" width="4.5" height="4.5" rx="1"/><rect x="15.6" y="4" width="4.5" height="4.5" rx="1"/><rect x="4" y="9.8" width="4.5" height="4.5" rx="1"/><rect x="9.8" y="9.8" width="4.5" height="4.5" rx="1"/><rect x="4" y="15.6" width="4.5" height="4.5" rx="1"/>',
        trend: '<path d="M4 16.5 9.5 11l3.5 3 5-6.5"/><path d="M15.5 7.5H20v4.5"/>',
        // —— 操作 ——
        search: '<circle cx="10.5" cy="10.5" r="5.5"/><path d="M14.6 14.6 20 20"/>',
        filter: '<path d="M4 6h16l-6 7v5l-4 2v-7L4 6Z"/>',
        columns: '<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M10 5v14M14.5 5v14"/>',
        sortAsc: '<path d="M8 17V6M8 6 5 9.5M8 6l3 3.5"/><path d="M14 17h6M14 12.5h4.5M14 8h3"/>',
        sortDesc: '<path d="M8 6v11M8 17l-3-3.5M8 17l3-3.5"/><path d="M14 6h6M14 10.5h4.5M14 15h3"/>',
        download: '<path d="M12 4v11M12 15l-3.5-3.5M12 15l3.5-3.5"/><path d="M5 19h14"/>',
        plus: '<path d="M12 5v14M5 12h14"/>',
        close: '<path d="M6 6l12 12M18 6 6 18"/>',
        check: '<path d="M5 12.5 10 17.5 19 7"/>',
        chevronDown: '<path d="M6 9.5 12 15.5 18 9.5"/>',
        chevronLeft: '<path d="M14.5 6 8.5 12l6 6"/>',
        chevronRight: '<path d="M9.5 6l6 6-6 6"/>',
        refresh: '<path d="M19 12a7 7 0 1 1-2.2-5.1"/><path d="M19.5 4v4.2h-4.2"/>',
        external: '<path d="M14 4.5h5.5V10"/><path d="M19.5 4.5 11 13"/><path d="M18 14.5V19a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 19V7.5A1.5 1.5 0 0 1 5 6h4.5"/>',
        trash: '<path d="M4.5 7h15"/><path d="M9.5 7V5.2A1.2 1.2 0 0 1 10.7 4h2.6a1.2 1.2 0 0 1 1.2 1.2V7"/><path d="M6.5 7l.8 12A1.5 1.5 0 0 0 8.8 20.5h6.4a1.5 1.5 0 0 0 1.5-1.5L17.5 7"/>',
        copy: '<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5V6A1.5 1.5 0 0 0 14 4.5H6A1.5 1.5 0 0 0 4.5 6v8A1.5 1.5 0 0 0 6 15.5h2.5"/>',
        play: '<path d="M8 5.5l10 6.5-10 6.5z"/>',
        bell: '<path d="M18 16.5V11a6 6 0 1 0-12 0v5.5L4.5 18.5h15L18 16.5Z"/><path d="M10 21h4"/>',
        user: '<circle cx="12" cy="8.5" r="3.4"/><path d="M5 20a7 7 0 0 1 14 0"/>',
        logout: '<path d="M15 5.5H18A1.5 1.5 0 0 1 19.5 7v10a1.5 1.5 0 0 1-1.5 1.5h-3"/><path d="M11 8.5 7.5 12 11 15.5"/><path d="M7.5 12h8"/>',
        clock: '<circle cx="12" cy="12" r="7.5"/><path d="M12 8v4.5l3 2"/>',
        alert: '<path d="M12 4.5 21 19.5H3L12 4.5Z"/><path d="M12 10v4M12 16.8v.2"/>',
        info: '<circle cx="12" cy="12" r="7.5"/><path d="M12 11v5.5M12 8.2v.2"/>',
        link: '<path d="M9.5 14.5 14.5 9.5"/><path d="M10.5 6.5l1.7-1.7a3.6 3.6 0 0 1 5.1 5.1L15.6 11.6"/><path d="M13.5 17.5l-1.7 1.7a3.6 3.6 0 0 1-5.1-5.1L8.4 12.4"/>',
        file: '<path d="M13.5 4.5H7A1.5 1.5 0 0 0 5.5 6v12A1.5 1.5 0 0 0 7 19.5h10A1.5 1.5 0 0 0 18.5 18V9.5L13.5 4.5Z"/><path d="M13.5 4.5V9.5h5"/>',
        spark: '<path d="M12 4l1.6 5.2L19 11l-5.4 1.8L12 18l-1.6-5.2L5 11l5.4-1.8L12 4Z"/>',
        robot: '<rect x="5" y="8" width="14" height="10" rx="3"/><path d="M12 4.5V8M9 13v1.5M15 13v1.5M8.5 18v2M15.5 18v2"/>',
        target: '<circle cx="12" cy="12" r="7.5"/><circle cx="12" cy="12" r="3.4"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3"/>',
        money: '<circle cx="12" cy="12" r="7.5"/><path d="M12 7.5v9M14.5 9.6c-.6-.9-1.6-1.3-2.7-1.3-1.4 0-2.4.7-2.4 1.8 0 2.6 5.4 1.3 5.4 4 0 1.2-1.1 2-2.6 2-1.2 0-2.2-.4-2.9-1.4"/>',
        city: '<path d="M4 20V9l5-3v14"/><path d="M9 20V4l5 3v13"/><path d="M14 20V10l5 2v8"/><path d="M2.5 20h19"/>',
        eye: '<path d="M2.8 12S6 6.5 12 6.5 21.2 12 21.2 12 18 17.5 12 17.5 2.8 12 2.8 12Z"/><circle cx="12" cy="12" r="2.6"/>'
    };
    function svg(name, size) {
        const s = size || 16;
        const d = P[name];
        if (!d) return '';
        return `<svg class="svg-ic" width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
    }
    function mount(root) {
        (root || document).querySelectorAll('[data-ic]:not([data-ic-done])').forEach(el => {
            const nm = el.getAttribute('data-ic');
            const sz = parseInt(el.getAttribute('data-sz') || '16', 10);
            const s = svg(nm, sz);
            if (s) { el.innerHTML = s; el.setAttribute('data-ic-done', '1'); }
        });
    }
    window.ICONS = { svg, mount, has: n => !!P[n] };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => mount());
    else mount();
})();
