/* ==========================================================================
 * 视频提取 · 剪辑压缩 —— 前端控制台（原生 ES2020，无框架 / 无构建 / 无外部依赖）
 *
 * 分区：
 *   1. state    —— 全局状态与常量
 *   2. utils    —— 通用工具函数（格式化 / DOM 构造 / 路径处理）
 *   3. api      —— 统一网络请求封装 + Toast 组件
 *   4. tabs     —— 标签页切换
 *   5. parse    —— 标签页 1：解析下载
 *   6. import   —— 标签页 2：本地导入
 *   7. edit     —— 标签页 3：剪辑压缩
 *   7b. watermark —— 去水印（B站投稿水印等）
 *   8. tasks    —— 底部任务面板 + SSE 事件流
 *   9. settings —— 设置弹窗
 *  10. init     —— 事件绑定与启动
 * ========================================================================== */
(function () {
  'use strict';

  /* ======================================================================
   * 1. state —— 全局状态与常量
   * ==================================================================== */

  var TASK_KIND_LABEL = {
    download: '下载', mux: '合并', trim: '裁剪',
    compress: '压缩', import: '导入', resolve: '解析'
  };

  var TASK_STATUS_LABEL = {
    pending: '排队中', running: '进行中', done: '已完成',
    error: '失败', canceled: '已取消'
  };

  var TASK_STATUS_DEFAULT_PHASE = {
    pending: '等待开始', running: '处理中', done: '已完成',
    error: '已失败', canceled: '已取消'
  };

  var TRIM_MODE_DESC = {
    smart: '智能无损：优先无损切割（流复制），自动把起止时间吸附到最近关键帧，速度最快且不损失画质。',
    lossless: '强制无损：完全流复制、不重新编码，速度极快；若起止点不在关键帧上，首尾画面可能有几帧偏差。',
    precise: '帧精确：重新编码，起止点精确到帧，速度较慢且画质有轻微损失。'
  };

  var CRF_DESC = {
    '18': 'CRF 18：接近视觉无损，体积约为源文件的 60% ~ 90%。',
    '21': 'CRF 21：推荐档位，画质与体积平衡，通常可压到源文件的 30% ~ 60%。',
    '24': 'CRF 24：体积优先，细节略有损失，适合快速分享与传阅。'
  };

  var WM_MODE_DESC = {
    auto: '自动（推荐）：背景平整的区域用插值修复（delogo），贴近画面边缘的区域自动改用模糊，避免出现无法插值的色块。',
    delogo: '插值修复：用区域四周的像素插值填补，画质最好，适合水印压在平整背景上的情况。',
    blur: '模糊：对区域做高斯模糊，适合水印压在复杂背景或贴边的情况；强度越高越干净，痕迹也越明显。',
    mosaic: '马赛克：对区域做像素化，遮盖最彻底但痕迹最重，适合高对比背景。'
  };

  // 去水印处理模式（与后端 watermark.MODES 保持一致）
  var WM_MODES = ['auto', 'delogo', 'blur', 'mosaic'];
  // 画布预览帧请求宽度（后端按此宽度等比缩放，坐标仍按原始像素记录）
  var WM_FRAME_W = 960;
  // 对比预览帧请求宽度
  var WM_CMP_W = 480;
  // 手动「添加区域」的默认尺寸（原始视频像素）
  var WM_ADD_W = 200;
  var WM_ADD_H = 60;
  // 框的最小边长（原始视频像素）
  var WM_MIN_SIZE = 6;
  // 拖动时间滑块后延迟多久真正取帧（毫秒）——避免一次拖动打出几十个请求
  var WM_FRAME_DELAY = 160;
  // 四个角的缩放手柄方向
  var WM_HANDLES = ['nw', 'ne', 'sw', 'se'];

  // 双滑块允许的最小片段长度（秒）
  var MIN_GAP = 0.1;
  // 关键帧吸附有效误差（秒）—— 用于提示文案
  var SNAP_TOLERANCE = 0.3;
  // 时间条上最多绘制多少个关键帧刻度
  var MAX_KF_TICKS = 400;
  // 本地乐观创建的任务在服务端列表缺席时最多保留多少秒
  var OPTIMISTIC_TTL = 30;
  // SSE 重连退避上限（毫秒）
  var SSE_MAX_BACKOFF = 15000;

  var state = {
    tab: 'parse',

    // 解析下载
    parsed: new Map(),        // media_id -> MediaItem
    cardNodes: new Map(),     // media_id -> { row, checkbox, streamSelect, audioRow, audioSelect }
    selected: new Set(),      // 已勾选的 media_id
    parsing: false,
    downloading: false,

    // 本地导入
    imported: [],             // MediaItem[]
    importing: false,

    // 剪辑压缩
    cur: {
      path: '',
      media: null,
      duration: 0,
      keyframes: null,        // number[] | null
      warn: ''
    },

    // 去水印（B站投稿水印等）
    wm: {
      boxes: [],              // { x, y, w, h, t0, t1 } —— 原始视频像素坐标 + 生效时间区间（秒）
      sel: -1,                // 当前选中的框下标
      drag: null,             // 进行中的拖拽 { mode, idx, handle, start, orig }
      loading: false,         // 预览帧是否正在加载
      frameErr: false,        // 预览帧加载失败
      errNotified: false,     // 是否已经就取帧失败弹过 toast（避免刷屏）
      detecting: false,       // 自动检测是否进行中
      timer: null             // 取帧防抖定时器
    },

    // 任务面板
    tasks: new Map(),         // task_id -> Task
    taskNodes: new Map(),     // task_id -> 行节点集合
    taskOrder: [],            // task_id，最新在前
    pendingHighlight: new Set(),
    optimistic: new Map(),    // task_id -> 本地乐观创建的任务（服务端列表尚未包含时保留）

    // 设置
    settings: null,

    // SSE
    es: null,
    esRetry: 0,
    esTimer: null,
    esEverConnected: false
  };

  /* ======================================================================
   * 2. utils —— 通用工具
   * ==================================================================== */

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  /** 极简 DOM 构造器：h('div', {class:'a', text:'x'}, child, ...) */
  function h(tag, attrs) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) return;
        if (k === 'class') node.className = v;
        else if (k === 'text') node.textContent = String(v);
        else if (k === 'dataset') Object.keys(v).forEach(function (dk) { node.dataset[dk] = v[dk]; });
        else if (k.slice(0, 2) === 'on' && typeof v === 'function') node.addEventListener(k.slice(2), v);
        else if (k === 'value') { node.setAttribute('value', v); node.value = v; }
        else if (v === true) node.setAttribute(k, '');
        else node.setAttribute(k, v);
      });
    }
    for (var i = 2; i < arguments.length; i++) {
      var children = arguments[i];
      var list = Array.isArray(children) ? children : [children];
      list.forEach(function (c) {
        if (c === null || c === undefined || c === false) return;
        node.appendChild(typeof c === 'object' && c.nodeType ? c : document.createTextNode(String(c)));
      });
    }
    return node;
  }

  var ICONS = {
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12.5l5 5L20 6.5"/></svg>',
    image: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4.5" width="18" height="15" rx="2.5"/><circle cx="8.5" cy="9.5" r="1.6"/><path d="M4 17l4.5-4.5 3.5 3.5 3-2.5L20 17"/></svg>',
    file: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M13 3H7.5A1.5 1.5 0 0 0 6 4.5v15A1.5 1.5 0 0 0 7.5 21h9a1.5 1.5 0 0 0 1.5-1.5V8z"/><path d="M13 3v5h5"/></svg>'
  };

  function icon(name) {
    var span = document.createElement('span');
    span.className = 'icon';
    span.innerHTML = ICONS[name] || '';
    span.style.display = 'inline-flex';
    span.style.width = '16px';
    span.style.height = '16px';
    var svg = span.firstChild;
    if (svg) { svg.style.width = '100%'; svg.style.height = '100%'; }
    return span;
  }

  function clamp(v, min, max) { return v < min ? min : (v > max ? max : v); }
  function num(v, fallback) { var n = parseFloat(v); return isFinite(n) ? n : (fallback || 0); }
  function round1(v) { return Math.round(num(v, 0) * 10) / 10; }

  /** 字节体积格式化：1536 -> 1.5 KB */
  function fmtBytes(bytes) {
    var n = Number(bytes);
    if (!isFinite(n) || n <= 0) return '—';
    var units = ['B', 'KB', 'MB', 'GB', 'TB'];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    var digits = n >= 100 ? 0 : (n >= 10 ? 1 : (i === 0 ? 0 : 2));
    return n.toFixed(digits) + ' ' + units[i];
  }

  /** 时长格式化：24 -> 00:24；3725 -> 1:02:05 */
  function fmtDuration(seconds) {
    var s = Number(seconds);
    if (!isFinite(s) || s < 0) return '--:--';
    s = Math.floor(s);
    var hh = Math.floor(s / 3600);
    var mm = Math.floor((s % 3600) / 60);
    var ss = s % 60;
    var pad = function (v) { return v < 10 ? '0' + v : String(v); };
    return hh > 0 ? (hh + ':' + pad(mm) + ':' + pad(ss)) : (pad(mm) + ':' + pad(ss));
  }

  /** 速度格式化：5242880 -> 5.00 MB/s */
  function fmtSpeed(bytesPerSec) {
    var n = Number(bytesPerSec);
    if (!isFinite(n) || n <= 0) return '';
    return fmtBytes(n) + '/s';
  }

  /** 剩余时间：12.5 -> 剩余 12s；95 -> 剩余 1m35s；3700 -> 剩余 1h1m */
  function fmtEta(seconds) {
    var s = Number(seconds);
    if (!isFinite(s) || s <= 0) return '';
    s = Math.round(s);
    if (s < 60) return '剩余 ' + s + 's';
    if (s < 3600) {
      var m = Math.floor(s / 60), rs = s % 60;
      return '剩余 ' + m + 'm' + (rs ? rs + 's' : '');
    }
    var hh = Math.floor(s / 3600), mm = Math.round((s % 3600) / 60);
    return '剩余 ' + hh + 'h' + (mm ? mm + 'm' : '');
  }

  /** 码率格式化：243567 -> 238.0 Kbps / 5.2 Mbps */
  function fmtBitrate(bps) {
    var n = Number(bps);
    if (!isFinite(n) || n <= 0) return '';
    if (n >= 1000 * 1000) return (n / 1000000).toFixed(2) + ' Mbps';
    return (n / 1000).toFixed(0) + ' Kbps';
  }

  /** 码率格式化（统一 Mbps，用于画质下拉框）：243567 -> 0.24 Mbps */
  function fmtMbps(bps) {
    var n = Number(bps);
    if (!isFinite(n) || n <= 0) return '';
    return (n / 1000000).toFixed(2) + ' Mbps';
  }

  function basename(p) {
    if (!p) return '';
    var s = String(p).replace(/[\\/]+$/, '');
    var idx = Math.max(s.lastIndexOf('\\'), s.lastIndexOf('/'));
    return idx >= 0 ? s.slice(idx + 1) : s;
  }

  function radioValue(name) {
    var el = document.querySelector('input[name="' + name + '"]:checked');
    return el ? el.value : '';
  }

  /** 把 video 元素 src 指向本地文件（路径必须 urlencode，否则含空格 / # 会失败） */
  function fileUrl(path) { return '/api/file?path=' + encodeURIComponent(path); }

  /** MediaItem -> 本地绝对路径（本地导入的条目可能是 file:// URL） */
  function itemLocalPath(item) {
    if (!item) return '';
    var p = item.path || item.local_path || item.file_path || item.source_url || '';
    p = String(p);
    if (/^file:\/\//i.test(p)) {
      p = p.replace(/^file:\/\//i, '');
      try { p = decodeURIComponent(p); } catch (e) { /* 保留原样 */ }
      if (/^\/[A-Za-z]:/.test(p)) p = p.slice(1);
      p = p.replace(/\//g, '\\');
    }
    return p;
  }

  /** 取出可用的视频轨（优先非音频轨） */
  function pickVideoStream(media) {
    var streams = (media && media.streams) || [];
    for (var i = 0; i < streams.length; i++) {
      if (!streams[i].is_audio_only) return streams[i];
    }
    return streams[0] || {};
  }

  /** 估算某条流的大小（字节）：优先 filesize，其次 码率 × 时长 */
  function estimateStreamSize(stream, duration) {
    if (!stream) return 0;
    if (Number(stream.filesize) > 0) return Number(stream.filesize);
    if (Number(stream.bandwidth) > 0 && Number(duration) > 0) {
      return Number(stream.bandwidth) * Number(duration) / 8;
    }
    return 0;
  }

  /** 画质下拉框里的文案 */
  function streamOptionLabel(stream, duration) {
    var parts = [];
    parts.push(stream.label || (stream.height ? stream.height + 'P' : '默认画质'));
    if (stream.width && stream.height) parts.push(stream.width + '×' + stream.height);
    if (Number(stream.fps) > 0) parts.push(round1(stream.fps) + 'fps');
    var br = fmtMbps(stream.bandwidth);
    if (br) parts.push(br);
    var size = estimateStreamSize(stream, duration);
    if (size > 0) parts.push('≈' + fmtBytes(size));
    if (stream.is_muxed) parts.push('含音轨');
    return parts.join(' · ');
  }

  /** 音轨下拉框里的文案 */
  function audioOptionLabel(audio) {
    var parts = [];
    parts.push(audio.label || '音轨');
    var br = fmtBitrate(audio.bandwidth);
    if (br) parts.push(br);
    if (audio.acodec) parts.push(audio.acodec);
    if (Number(audio.filesize) > 0) parts.push(fmtBytes(audio.filesize));
    return parts.join(' · ');
  }

  /** 首帧 / 无封面时的占位（内联 SVG，无外部资源） */
  function coverPlaceholder() {
    var ph = h('div', { class: 'cover-ph' });
    ph.appendChild(icon('image'));
    ph.appendChild(h('span', { text: '暂无封面' }));
    return ph;
  }

  function setHint(sel, text, cls) {
    var el = $(sel);
    if (!el) return;
    el.textContent = text || '';
    el.className = 'hint' + (cls ? ' ' + cls : '');
  }

  /* ======================================================================
   * 3a. api —— 统一请求封装
   * ==================================================================== */

  function ApiError(message, status, path, data) {
    this.name = 'ApiError';
    this.message = message;
    this.status = status || 0;
    this.path = path || '';
    this.data = data || null;
  }
  ApiError.prototype = Object.create(Error.prototype);

  /** 从响应体里提取一句具体的错误原因 */
  function errorFromResponse(data, raw, status) {
    if (data && typeof data === 'object') {
      var d = data.detail;
      if (typeof d === 'string' && d.trim()) return d.trim();
      if (Array.isArray(d) && d.length) {
        var first = d[0];
        if (first && typeof first.msg === 'string') {
          var loc = Array.isArray(first.loc) ? first.loc.filter(function (x) { return x !== 'body'; }).join('.') : '';
          return '参数不合法' + (loc ? '（' + loc + '）' : '') + '：' + first.msg;
        }
        return '参数不合法：' + JSON.stringify(first);
      }
      if (typeof data.error === 'string' && data.error.trim()) return data.error.trim();
      if (typeof data.message === 'string' && data.message.trim()) return data.message.trim();
    }
    if (typeof raw === 'string' && raw.trim() && raw.length < 300 && raw.trim().charAt(0) !== '<') {
      return raw.trim();
    }
    if (status === 404) return '接口不存在（404），请确认后端版本与前端匹配';
    if (status === 405) return '请求方法不被允许（405）';
    if (status >= 500) return '服务内部错误（' + status + '）';
    return '请求失败（HTTP ' + status + '）';
  }

  /**
   * 统一请求：自动 JSON 序列化、非 2xx 抛 ApiError。
   * api(path)                     -> GET
   * api(path, body)               -> POST JSON
   * api(path, body, {method:'POST'}) / api(path, null, {method:'POST'})
   */
  async function api(path, body, opts) {
    opts = opts || {};
    var hasBody = body !== undefined && body !== null;
    var init = {
      method: opts.method || (hasBody ? 'POST' : 'GET'),
      headers: {},
      cache: 'no-store'
    };
    if (hasBody) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    var res;
    try {
      res = await fetch(path, init);
    } catch (err) {
      var reason = (err && err.message) ? err.message : '网络错误';
      throw new ApiError('无法连接本地服务（' + reason + '），请确认后端已启动', 0, path);
    }
    var raw = '';
    try { raw = await res.text(); } catch (e) { raw = ''; }
    var data = null;
    if (raw) {
      try { data = JSON.parse(raw); } catch (e) { data = null; }
    }
    if (!res.ok) throw new ApiError(errorFromResponse(data, raw, res.status), res.status, path, data);
    return data === null ? {} : data;
  }

  /** 错误数组（{url,error} / {media_id,error}）里的第一条转成可读文案 */
  function errText(entry) {
    if (!entry) return '未知错误';
    if (typeof entry === 'string') return entry;
    var who = entry.url || entry.media_id || entry.path || entry.name || '';
    var why = entry.error || entry.detail || entry.message || JSON.stringify(entry);
    return (who ? who + '：' : '') + why;
  }

  /* ======================================================================
   * 3b. Toast —— 右上角堆叠提示
   * ==================================================================== */

  var TOAST_ICON = { info: 'ℹ', success: '✓', warn: '⚠', error: '✕' };
  var TOAST_TITLE = { info: '提示', success: '完成', warn: '注意', error: '出错了' };
  // 最多同时堆叠的 toast 数量（超出时立刻丢弃最旧的）
  var MAX_TOASTS = 6;

  /**
   * 关闭一个 toast。
   * immediate = true 时同步移除节点（用于超出堆叠上限时的裁剪，必须保证循环能推进）。
   */
  function removeToast(node, immediate) {
    if (!node || !node.parentNode || node.dataset.closing) return;
    node.dataset.closing = '1';
    if (immediate) {
      node.parentNode.removeChild(node);
      return;
    }
    node.classList.add('is-out');
    setTimeout(function () {
      if (node.parentNode) node.parentNode.removeChild(node);
    }, 200);
  }

  function toast(message, type, opts) {
    type = TOAST_ICON[type] ? type : 'info';
    opts = opts || {};
    var box = $('#toasts');
    if (!box) { console.warn('[toast]', type, message); return null; }
    var node = h('div', { class: 'toast toast-' + type },
      h('span', { class: 'toast-icon', text: TOAST_ICON[type] }),
      h('div', { class: 'toast-body' },
        h('div', { class: 'toast-title', text: opts.title || TOAST_TITLE[type] }),
        h('div', { class: 'toast-msg', text: message === null || message === undefined ? '' : String(message) })
      )
    );
    node.addEventListener('click', function () { removeToast(node); });
    box.appendChild(node);
    while (box.children.length > MAX_TOASTS) removeToast(box.firstElementChild, true);
    var ttl = opts.duration || (type === 'error' ? 6000 : 3000);
    setTimeout(function () { removeToast(node); }, ttl);
    return node;
  }

  /* ======================================================================
   * 4. tabs —— 标签页
   * ==================================================================== */

  function activateTab(name) {
    var valid = ['parse', 'import', 'edit'];
    if (valid.indexOf(name) < 0) name = 'parse';
    state.tab = name;
    $$('#tabs .tab').forEach(function (btn) {
      var on = btn.dataset.tab === name;
      btn.classList.toggle('is-active', on);
      btn.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    valid.forEach(function (n) {
      var page = $('#page-' + n);
      if (page) page.classList.toggle('is-active', n === name);
    });
    var content = $('#content');
    if (content) content.scrollTop = 0;
    try { history.replaceState(null, '', '#' + name); } catch (e) { /* file:// 下忽略 */ }
  }

  /* ======================================================================
   * 5. parse —— 标签页 1：解析下载
   * ==================================================================== */

  function setBtnLabel(btn, text) {
    if (!btn) return;
    var lb = btn.querySelector('.btn-label');
    if (lb) { lb.textContent = text; btn.dataset.idleLabel = text; }
  }

  function setBusy(btn, busy, busyLabel) {
    if (!btn) return;
    var sp = btn.querySelector('.spinner');
    var lb = btn.querySelector('.btn-label');
    if (lb && !btn.dataset.idleLabel) btn.dataset.idleLabel = lb.textContent;
    btn.disabled = !!busy;
    if (sp) sp.hidden = !busy;
    if (lb) lb.textContent = busy ? (busyLabel || btn.dataset.idleLabel) : btn.dataset.idleLabel;
  }

  async function doParse() {
    if (state.parsing) return;
    var input = $('#parse-input');
    var text = (input.value || '').trim();
    if (!text) {
      toast('请先把分享文本粘贴到输入框里', 'warn');
      input.focus();
      return;
    }
    state.parsing = true;
    var btn = $('#btn-parse');
    setBusy(btn, true, '解析中…');
    setHint('#parse-status', '正在解析链接，请稍候…');
    try {
      var res = await api('/api/parse', { text: text });
      renderParseResult(res);
    } catch (e) {
      setHint('#parse-status', '');
      toast('解析失败：' + e.message, 'error');
    } finally {
      state.parsing = false;
      setBusy(btn, false);
    }
  }

  function renderParseResult(res) {
    res = res || {};
    var items = Array.isArray(res.items) ? res.items : [];
    var unrecognized = Array.isArray(res.unrecognized) ? res.unrecognized : [];
    var errors = Array.isArray(res.errors) ? res.errors : [];
    var found = Number(res.found);
    if (!isFinite(found)) found = items.length;

    state.parsed.clear();
    state.cardNodes.clear();
    state.selected.clear();
    var grid = $('#parse-results');
    grid.textContent = '';

    items.forEach(function (item) {
      if (!item || !item.id) return;
      state.parsed.set(item.id, item);
      grid.appendChild(buildMediaCard(item));
    });

    // 未识别链接
    var unBox = $('#unrecognized-box');
    var unList = $('#unrecognized-list');
    unList.textContent = '';
    if (unrecognized.length) {
      unrecognized.forEach(function (u) { unList.appendChild(h('li', { text: String(u) })); });
      $('#unrecognized-count').textContent = String(unrecognized.length);
      unBox.hidden = false;
    } else {
      unBox.hidden = true;
    }

    // 解析错误
    var errBox = $('#parse-errors');
    var errList = $('#parse-errors-list');
    errList.textContent = '';
    if (errors.length) {
      errors.forEach(function (e) {
        errList.appendChild(h('li', null,
          h('span', { text: (e && (e.url || e.media_id)) ? String(e.url || e.media_id) + ' ' : '' }),
          h('span', { class: 'err-msg', text: '解析失败：' + ((e && (e.error || e.detail)) || '未知原因') })
        ));
      });
      $('#parse-errors-count').textContent = String(errors.length);
      errBox.hidden = false;
    } else {
      errBox.hidden = true;
    }

    $('#parse-empty').hidden = items.length > 0;
    $('#parse-toolbar').hidden = items.length === 0;
    $('#parse-count').textContent = '找到 ' + found + ' 个媒体' +
      (unrecognized.length ? '，' + unrecognized.length + ' 条链接未识别' : '') +
      (errors.length ? '，' + errors.length + ' 条解析出错' : '');

    if (!items.length) {
      if (unrecognized.length || errors.length) {
        toast('没有解析出可用媒体：请检查链接是否为受支持的抖音 / B 站链接', 'warn');
      } else {
        toast('没有在文本里找到可识别的链接', 'warn');
      }
    } else {
      toast('解析完成，共找到 ' + items.length + ' 个媒体', 'success');
    }

    setHint('#parse-status', items.length ? '解析完成' : '');
    updateSelectionUI();
  }

  function buildMediaCard(item) {
    var checkbox = h('input', { type: 'checkbox', 'aria-label': '选择该媒体' });
    var duration = Number(item.duration) || 0;
    var isImages = item.kind === 'images';
    var images = Array.isArray(item.images) ? item.images : [];
    var streams = (Array.isArray(item.streams) ? item.streams : []).filter(function (s) { return !s.is_audio_only; });
    var audios = Array.isArray(item.audios) ? item.audios : [];

    // ---- 封面 ----
    var cover = h('div', { class: 'card-cover' });
    cover.appendChild(coverPlaceholder());
    if (item.cover) {
      var img = h('img', { src: item.cover, alt: item.title || '封面', loading: 'lazy', referrerpolicy: 'no-referrer' });
      img.addEventListener('error', function () {
        img.remove();
        if (!cover.querySelector('.cover-ph')) cover.appendChild(coverPlaceholder());
      });
      img.addEventListener('load', function () {
        var ph = cover.querySelector('.cover-ph');
        if (ph) ph.remove();
      });
      cover.appendChild(img);
    }
    cover.appendChild(h('span', { class: 'badge-platform', text: item.platform_name || item.platform || '未知平台' }));
    if (isImages) {
      cover.appendChild(h('span', { class: 'badge-kind', text: '图集 · ' + (images.length || item.image_count || 0) + ' 张' }));
    } else if (duration > 0) {
      cover.appendChild(h('span', { class: 'dur', text: fmtDuration(duration) }));
    }

    // ---- 正文 ----
    var body = h('div', { class: 'card-body' },
      h('div', { class: 'card-title', title: item.title || '', text: item.title || '(无标题)' })
    );
    if (item.author) body.appendChild(h('div', { class: 'card-author', text: 'UP：' + item.author }));

    var warnings = Array.isArray(item.warnings) ? item.warnings : [];
    if (warnings.length) {
      var warnBox = h('div', { class: 'card-warns' });
      warnings.forEach(function (w) { warnBox.appendChild(h('div', { class: 'warn-line', text: '⚠ ' + w })); });
      body.appendChild(warnBox);
    }

    var fields = h('div', { class: 'card-fields' });
    var streamSelect = null;
    var audioSelect = null;
    var audioRow = null;

    if (isImages) {
      fields.appendChild(h('div', { class: 'card-note', text: '图集类型，将按原图打包下载（无需选择画质）。' }));
    } else if (!streams.length) {
      fields.appendChild(h('div', { class: 'card-note', text: '未获取到可下载画质，可能需要在设置中配置 Cookie 后重新解析。' }));
    } else {
      streamSelect = h('select', { class: 'select', 'aria-label': '选择画质' });
      // 默认选最高画质（分辨率优先，其次码率）
      var best = streams.slice().sort(function (a, b) {
        return (Number(b.height) || 0) - (Number(a.height) || 0) || (Number(b.bandwidth) || 0) - (Number(a.bandwidth) || 0);
      })[0];
      streams.forEach(function (s) {
        var opt = h('option', {
          value: s.stream_id,
          text: streamOptionLabel(s, duration),
          dataset: { muxed: s.is_muxed ? '1' : '0' }
        });
        if (best && s.stream_id === best.stream_id) opt.selected = true;
        streamSelect.appendChild(opt);
      });
      fields.appendChild(h('label', { class: 'field' }, h('span', { text: '画质' }), streamSelect));

      audioRow = h('label', { class: 'field' },
        h('span', { text: '音轨' }),
        (audioSelect = h('select', { class: 'select', 'aria-label': '选择音轨' }))
      );
      if (!audios.length) {
        audioSelect.appendChild(h('option', { value: '', text: '无独立音轨（该画质自带音频）' }));
      } else {
        audios.forEach(function (a) {
          audioSelect.appendChild(h('option', { value: a.stream_id, text: audioOptionLabel(a) }));
        });
      }
      fields.appendChild(audioRow);
    }
    body.appendChild(fields);

    var row = h('div', { class: 'card', 'data-id': item.id },
      h('label', { class: 'card-check', title: '选择该媒体' }, checkbox),
      cover, body
    );

    var node = {
      row: row,
      item: item,
      checkbox: checkbox,
      streamSelect: streamSelect,
      audioRow: audioRow,
      audioSelect: audioSelect
    };
    state.cardNodes.set(item.id, node);

    // 勾选
    checkbox.addEventListener('change', function () {
      if (checkbox.checked) state.selected.add(item.id);
      else state.selected.delete(item.id);
      row.classList.toggle('is-selected', checkbox.checked);
      updateSelectionUI();
    });

    // 画质切换 -> 是否需要选音轨
    if (streamSelect) {
      var syncAudio = function () {
        var opt = streamSelect.options[streamSelect.selectedIndex];
        var muxed = opt && opt.dataset.muxed === '1';
        if (audioRow) audioRow.hidden = !!muxed;
      };
      streamSelect.addEventListener('change', syncAudio);
      syncAudio();
    }

    return row;
  }

  function updateSelectionUI() {
    var btn = $('#btn-download');
    var count = state.selected.size;
    setBtnLabel(btn, '下载选中(' + count + ')');
    if (!state.downloading) btn.disabled = count === 0;
  }

  function collectDownloadItems() {
    var items = [];
    state.selected.forEach(function (id) {
      var item = state.parsed.get(id);
      var node = state.cardNodes.get(id);
      if (!item) return;
      var streamId = null;
      var audioId = null;
      if (node && node.streamSelect && node.streamSelect.value) streamId = node.streamSelect.value;
      if (node && node.audioSelect && node.audioRow && !node.audioRow.hidden && node.audioSelect.value) {
        audioId = node.audioSelect.value;
      }
      items.push({ media_id: item.id, stream_id: streamId, audio_id: audioId });
    });
    return items;
  }

  async function doDownload() {
    if (state.downloading) return;
    var items = collectDownloadItems();
    if (!items.length) {
      toast('请先勾选要下载的媒体', 'warn');
      return;
    }
    state.downloading = true;
    var btn = $('#btn-download');
    setBusy(btn, true, '提交中…');
    try {
      var res = await api('/api/download', { items: items });
      var ids = Array.isArray(res.task_ids) ? res.task_ids : [];
      var errors = Array.isArray(res.errors) ? res.errors : [];
      ids.forEach(function (id) { state.pendingHighlight.add(id); });
      if (ids.length) {
        toast('已提交 ' + ids.length + ' 个下载任务', 'success');
        focusTaskPanel();
        refreshTasks();
      }
      if (errors.length) {
        toast('有 ' + errors.length + ' 个任务提交失败：' + errText(errors[0]), 'error');
      }
      if (!ids.length && !errors.length) {
        toast('后端没有返回任何下载任务，请确认链接是否仍然有效', 'warn');
      }
    } catch (e) {
      toast('提交下载失败：' + e.message, 'error');
    } finally {
      state.downloading = false;
      setBusy(btn, false);
      updateSelectionUI();
    }
  }

  /* ======================================================================
   * 6. import —— 标签页 2：本地导入
   * ==================================================================== */

  function setImportBusy(busy) {
    state.importing = !!busy;
    var dz = $('#dropzone');
    var tip = $('#import-busy');
    if (dz) dz.classList.toggle('is-busy', state.importing);
    if (tip) tip.hidden = !state.importing;
  }

  async function pickLocalFiles() {
    if (state.importing) return;
    setImportBusy(true);
    try {
      var res = await api('/api/import/pick', {});
      var items = Array.isArray(res.items) ? res.items : [];
      if (!items.length) {
        toast('没有选择任何文件', 'info');
        return;
      }
      addImportedItems(items);
      toast('已导入 ' + items.length + ' 个文件', 'success');
    } catch (e) {
      toast('调起文件选择框失败：' + e.message, 'error');
    } finally {
      setImportBusy(false);
    }
  }

  async function importByPaths(paths) {
    if (state.importing) return;
    if (!paths || !paths.length) {
      toast('没有识别到可导入的文件路径', 'warn');
      return;
    }
    setImportBusy(true);
    try {
      var res = await api('/api/import/paths', { paths: paths });
      var items = Array.isArray(res.items) ? res.items : [];
      var errors = Array.isArray(res.errors) ? res.errors : [];
      if (items.length) {
        addImportedItems(items);
        toast('已导入 ' + items.length + ' 个文件', 'success');
      }
      if (errors.length) {
        toast('有 ' + errors.length + ' 个文件导入失败：' + errText(errors[0]), 'error');
      }
      if (!items.length && !errors.length) {
        toast('没有导入任何文件，请确认文件是否为受支持的视频格式', 'warn');
      }
    } catch (e) {
      toast('导入失败：' + e.message, 'error');
    } finally {
      setImportBusy(false);
    }
  }

  function addImportedItems(items) {
    var empty = $('#import-empty');
    if (empty) empty.hidden = true;
    var grid = $('#import-results');
    items.forEach(function (item) {
      if (!item) return;
      state.imported.push(item);
      grid.insertBefore(buildFileCard(item), grid.firstChild);
    });
  }

  function mediaSpecList(item) {
    var st = pickVideoStream(item);
    var w = Number(item.width) || Number(st.width) || 0;
    var hgt = Number(item.height) || Number(st.height) || 0;
    var size = Number(item.filesize) || Number(item.size) || Number(st.filesize) || 0;
    var bitrate = Number(item.bitrate) || Number(st.bandwidth) || 0;
    var specs = [];
    specs.push(['时长', fmtDuration(item.duration)]);
    specs.push(['分辨率', (w && hgt) ? w + '×' + hgt : '—']);
    specs.push(['编码', st.vcodec || st.codec_family || '—']);
    if (Number(st.fps) > 0) specs.push(['帧率', round1(st.fps) + 'fps']);
    specs.push(['码率', bitrate ? fmtBitrate(bitrate) : '—']);
    specs.push(['体积', size ? fmtBytes(size) : '—']);
    return specs;
  }

  function buildFileCard(item) {
    var path = itemLocalPath(item);
    var name = item.title || basename(path) || '(未命名文件)';
    var specs = mediaSpecList(item);

    var specBox = h('div', { class: 'filecard-specs' });
    specs.forEach(function (pair) {
      specBox.appendChild(h('span', { class: 'spec' },
        h('span', { text: pair[0] }),
        h('b', { text: String(pair[1]) })
      ));
    });

    var btnEdit = h('button', { class: 'btn btn-primary btn-sm', type: 'button', text: '去剪辑' });
    btnEdit.addEventListener('click', function () {
      if (!path) { toast('该文件没有可用的本地路径，无法进入剪辑', 'error'); return; }
      goToEdit(path);
    });

    var btnReveal = h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '打开所在文件夹' });
    btnReveal.addEventListener('click', function () {
      if (!path) { toast('该文件没有可用的本地路径', 'error'); return; }
      api('/api/reveal', { path: path })
        .then(function () { toast('已在资源管理器中定位文件', 'success'); })
        .catch(function (e) { toast('打开文件夹失败：' + e.message, 'error'); });
    });

    var card = h('div', { class: 'filecard' },
      h('div', { class: 'filecard-name' }, icon('file'), h('span', { text: name })),
      h('div', { class: 'card-author mono', title: path, text: path }),
      specBox,
      h('div', { class: 'filecard-actions' }, btnEdit, btnReveal)
    );

    var warnings = Array.isArray(item.warnings) ? item.warnings : [];
    if (warnings.length) {
      var warnBox = h('div', { class: 'card-warns' });
      warnings.forEach(function (w) { warnBox.appendChild(h('div', { class: 'warn-line', text: '⚠ ' + w })); });
      card.insertBefore(warnBox, specBox);
    }
    return card;
  }

  function goToEdit(path) {
    activateTab('edit');
    loadEditPath(path);
  }

  /* ======================================================================
   * 7. edit —— 标签页 3：剪辑压缩
   * ==================================================================== */

  function renderSrcWarn() {
    var msgs = [];
    if (state.settings && state.settings.has_ffmpeg === false) {
      msgs.push('未检测到 FFmpeg：剪辑与压缩无法执行，请将 ffmpeg.exe 放入 vendor/ffmpeg 目录。');
    }
    if (state.cur.warn) msgs.push(state.cur.warn);
    var el = $('#src-warn');
    if (!el) return;
    if (!msgs.length) { el.hidden = true; el.textContent = ''; return; }
    el.hidden = false;
    el.textContent = msgs.join('　');
  }

  function setPreviewSrc(path) {
    var v = $('#preview');
    var empty = $('#player-empty');
    if (empty) empty.hidden = !!path;
    if (!path) { v.removeAttribute('src'); v.load(); return; }
    v.src = fileUrl(path);
    try { v.load(); } catch (e) { /* 忽略 */ }
  }

  /** 当前文件信息（后端 probe 结果 + video 元素兜底） */
  function currentMediaInfo() {
    var m = state.cur.media || {};
    var st = pickVideoStream(m);
    var v = $('#preview');
    var vw = Number(v.videoWidth) || 0;
    var vh = Number(v.videoHeight) || 0;
    var vd = (isFinite(v.duration) && v.duration > 0) ? v.duration : 0;
    var audios = Array.isArray(m.audios) ? m.audios : [];
    return {
      title: m.title || basename(state.cur.path) || '(未命名文件)',
      path: state.cur.path,
      duration: Number(m.duration) || vd || 0,
      width: Number(m.width) || Number(st.width) || vw || 0,
      height: Number(m.height) || Number(st.height) || vh || 0,
      vcodec: st.vcodec || st.codec_family || '',
      acodec: (audios[0] && audios[0].acodec) || '',
      fps: Number(st.fps) || 0,
      bitrate: Number(m.bitrate) || Number(st.bandwidth) || 0,
      size: Number(m.filesize) || Number(m.size) || Number(st.filesize) || 0,
      author: m.author || '',
      platform: m.platform_name || m.platform || ''
    };
  }

  function renderSourceInfo() {
    var box = $('#src-info');
    box.textContent = '';
    if (!state.cur.path) {
      box.appendChild(h('div', { class: 'kv-row' },
        h('span', { class: 'kv-key', text: '状态' }),
        h('span', { class: 'kv-val muted', text: '等待载入文件…' })
      ));
      return;
    }
    var info = currentMediaInfo();
    var rows = [
      ['文件名', info.title],
      ['路径', info.path, true],
      ['时长', info.duration > 0 ? fmtDuration(info.duration) + '（' + info.duration.toFixed(1) + 's）' : '—'],
      ['分辨率', (info.width && info.height) ? info.width + '×' + info.height : '—'],
      ['视频编码', info.vcodec || '—'],
      ['音频编码', info.acodec || '—'],
      ['帧率', info.fps > 0 ? round1(info.fps) + ' fps' : '—'],
      ['码率', info.bitrate > 0 ? fmtBitrate(info.bitrate) : '—'],
      ['体积', info.size > 0 ? fmtBytes(info.size) : '—']
    ];
    if (info.platform || info.author) {
      rows.push(['来源', [info.platform, info.author].filter(Boolean).join(' · ')]);
    }
    rows.forEach(function (r) {
      box.appendChild(h('div', { class: 'kv-row' },
        h('span', { class: 'kv-key', text: r[0] }),
        h('span', { class: 'kv-val' + (r[2] ? ' mono' : ''), title: String(r[1]), text: String(r[1]) })
      ));
    });
  }

  async function loadEditPath(path) {
    path = String(path || '').trim().replace(/^"|"$/g, '');
    if (!path) {
      toast('请先选择文件或输入文件路径', 'warn');
      return;
    }
    $('#edit-path').value = path;
    state.cur.path = path;
    state.cur.media = null;
    state.cur.duration = 0;
    state.cur.keyframes = null;
    state.cur.warn = '';
    renderSrcWarn();
    renderKeyframeTicks();
    setPreviewSrc(path);
    resetTrimRange(0);
    // 去水印：换了文件，之前的框不再适用
    wmOnSourceChanged(path);
    setHint('#kf-status', '正在读取关键帧…');
    renderSourceInfo();
    await probePath(path);
    loadKeyframes(path);
  }

  async function probePath(path) {
    try {
      var res = await api('/api/probe?path=' + encodeURIComponent(path));
      if (state.cur.path !== path) return;
      var media = (res && res.media) ? res.media : res;
      state.cur.media = media || null;
      var dur = Number(media && media.duration) || 0;
      var warnings = (media && Array.isArray(media.warnings)) ? media.warnings : [];
      state.cur.warn = warnings.length ? warnings.join('；') : '';
      renderSrcWarn();
      renderSourceInfo();
      resetTrimRange(dur);
      if (!(dur > 0)) {
        toast('未能读取到时长：裁剪滑块不可用，可手动输入起止时间', 'warn');
      }
    } catch (e) {
      if (state.cur.path !== path) return;
      toast('读取文件信息失败：' + e.message, 'error');
      state.cur.warn = '文件信息读取失败：' + e.message;
      renderSrcWarn();
    }
  }

  async function loadKeyframes(path) {
    var target = path || state.cur.path;
    if (!target) return;
    state.cur.keyframes = null;
    renderKeyframeTicks();
    setHint('#kf-status', '正在读取关键帧…');
    try {
      var res = await api('/api/keyframes?path=' + encodeURIComponent(target));
      if (state.cur.path !== target) return;
      var raw = Array.isArray(res && res.keyframes) ? res.keyframes : [];
      var kfs = raw
        .map(function (v) { return Number(v); })
        .filter(function (v) { return isFinite(v) && v >= 0; })
        .sort(function (a, b) { return a - b; });
      state.cur.keyframes = kfs;
      if (Number(res && res.duration) > 0 && !(state.cur.duration > 0)) {
        resetTrimRange(Number(res.duration));
      }
      renderKeyframeTicks();
      if (kfs.length) {
        setHint('#kf-status', '已获取 ' + kfs.length + ' 个关键帧', 'hint-accent');
      } else {
        setHint('#kf-status', '未获取到关键帧，智能模式将不吸附');
      }
    } catch (e) {
      if (state.cur.path !== target) return;
      state.cur.keyframes = null;
      renderKeyframeTicks();
      setHint('#kf-status', '关键帧获取失败，已跳过硬吸附（可手动输入时间）', 'hint-warn');
    }
  }

  function renderKeyframeTicks() {
    var layer = $('#trim-kf');
    if (!layer) return;
    layer.textContent = '';
    var kfs = state.cur.keyframes;
    var d = state.cur.duration;
    if (!kfs || !kfs.length || !(d > 0)) return;
    var stride = Math.ceil(kfs.length / MAX_KF_TICKS);
    for (var i = 0; i < kfs.length; i += stride) {
      var t = kfs[i];
      if (!(t >= 0) || t > d) continue;
      layer.appendChild(h('span', {
        class: 'range-tick',
        style: 'left:' + (t / d * 100).toFixed(3) + '%',
        title: '关键帧 ' + t.toFixed(1) + 's'
      }));
    }
  }

  function trimRangeEls() {
    return {
      sr: $('#trim-start-range'),
      er: $('#trim-end-range'),
      si: $('#trim-start'),
      ei: $('#trim-end')
    };
  }

  function resetTrimRange(duration) {
    var d = Number(duration) || 0;
    state.cur.duration = d;
    var el = trimRangeEls();
    var usable = d > 0;
    el.sr.max = usable ? d : 1;
    el.er.max = usable ? d : 1;
    el.sr.value = 0;
    el.er.value = usable ? d : 1;
    el.si.value = '0.0';
    el.ei.value = usable ? d.toFixed(1) : '0.0';
    setHint('#trim-snap-hint', '');
    renderKeyframeTicks();
    syncTrimUI();
    // 时长变化会影响去水印的时间滑块（未知时长时滑块退化）
    wmSyncUI();
  }

  function trimEnabled() {
    var cb = $('#trim-enabled');
    return !!(cb && cb.checked);
  }

  /** 刷新进度条填充与片段时长文案 */
  function updateFillAndLength() {
    var el = trimRangeEls();
    var d = state.cur.duration || 0;
    var a = num(el.sr.value, 0);
    var b = num(el.er.value, 0);
    var pct = function (v) { return d > 0 ? clamp(v / d * 100, 0, 100) : 0; };
    var fill = $('#trim-fill');
    fill.style.left = pct(a) + '%';
    fill.style.width = Math.max(0, pct(b) - pct(a)) + '%';
    // 两个滑块接近重叠时让起点滑块浮到上层，否则无法拖动
    el.sr.style.zIndex = (d > 0 && a > d / 2) ? '4' : '2';
    el.er.style.zIndex = '3';
    $('#trim-length').textContent = d > 0
      ? '片段时长 ' + Math.max(0, b - a).toFixed(1) + 's / 共 ' + d.toFixed(1) + 's'
      : '片段时长 —（未读取到时长）';
  }

  function syncTrimUI() {
    var el = trimRangeEls();
    var d = state.cur.duration || 0;
    var on = trimEnabled();
    var usable = on && d > 0;
    el.sr.disabled = !usable;
    el.er.disabled = !usable;
    el.si.disabled = !on;
    el.ei.disabled = !on;
    // 只要读到时长就允许用「设起点 / 设终点」，点击时会自动开启裁剪
    $('#btn-set-start').disabled = !(d > 0);
    $('#btn-set-end').disabled = !(d > 0);
    updateFillAndLength();
  }

  /** 数字输入框 -> 滑块（输入过程中不回写输入框，避免光标跳动） */
  function syncFromNumberInputs() {
    var el = trimRangeEls();
    var d = state.cur.duration || 0;
    if (!(d > 0)) return;
    var a = clamp(num(el.si.value, 0), 0, d);
    var b = clamp(num(el.ei.value, d), 0, d);
    el.sr.value = clamp(Math.min(a, Math.max(0, b - MIN_GAP)), 0, d);
    el.er.value = clamp(Math.max(b, a + MIN_GAP), 0, d);
    updateFillAndLength();
  }

  /** 滑块 -> 数字输入框 */
  function syncFromRanges(which) {
    var el = trimRangeEls();
    var d = state.cur.duration || 0;
    if (!(d > 0)) return;
    var a = clamp(num(el.sr.value, 0), 0, d);
    var b = clamp(num(el.er.value, d), 0, d);
    if (b - a < MIN_GAP) {
      if (which === 'end') b = clamp(a + MIN_GAP, 0, d);
      else a = clamp(b - MIN_GAP, 0, d);
    }
    el.sr.value = a;
    el.er.value = b;
    el.si.value = a.toFixed(1);
    el.ei.value = b.toFixed(1);
    updateFillAndLength();
  }

  /** 智能模式下把时间吸附到最近关键帧 */
  function snapToKeyframe(value) {
    var kfs = state.cur.keyframes;
    if (radioValue('trim-mode') !== 'smart' || !kfs || !kfs.length) {
      setHint('#trim-snap-hint', '');
      return value;
    }
    var best = null, bestD = Infinity;
    for (var i = 0; i < kfs.length; i++) {
      var delta = Math.abs(kfs[i] - value);
      if (delta < bestD) { bestD = delta; best = kfs[i]; }
    }
    if (best === null) return value;
    if (bestD <= 0.001) { setHint('#trim-snap-hint', ''); return value; }
    setHint('#trim-snap-hint',
      bestD <= SNAP_TOLERANCE
        ? '已吸附到关键帧（误差 ±0.3s）'
        : '已吸附到最近关键帧（误差 ±' + bestD.toFixed(1) + 's）',
      'hint-accent');
    return Math.round(best * 10) / 10;
  }

  /**
   * 写入裁剪区间。
   * opts: { snapStart, snapEnd } —— 是否执行关键帧吸附
   */
  function applyTrimRange(a, b, opts) {
    opts = opts || {};
    var el = trimRangeEls();
    var d = state.cur.duration || 0;
    if (!(d > 0)) {
      el.si.value = round1(a).toFixed(1);
      el.ei.value = round1(b).toFixed(1);
      return;
    }
    var x = clamp(num(a, 0), 0, d);
    var y = clamp(num(b, d), 0, d);
    if (!opts.skipSnap) {
      if (opts.snapStart) x = clamp(snapToKeyframe(x), 0, d);
      if (opts.snapEnd) y = clamp(snapToKeyframe(y), 0, d);
    }
    if (y - x < MIN_GAP) {
      if (opts.snapEnd || !opts.snapStart) y = Math.min(d, x + MIN_GAP);
      else x = Math.max(0, y - MIN_GAP);
    }
    el.sr.value = x;
    el.er.value = y;
    el.si.value = x.toFixed(1);
    el.ei.value = y.toFixed(1);
    syncTrimUI();
  }

  /** 使用播放位置设置起止点时，自动开启裁剪开关 */
  function ensureTrimOn() {
    var cb = $('#trim-enabled');
    if (cb && !cb.checked) {
      cb.checked = true;
      syncTrimUI();
    }
  }

  function setStartFromPlayer() {
    var v = $('#preview');
    if (!(state.cur.duration > 0) && !(v.duration > 0)) {
      toast('还没有可用的视频，请先载入文件', 'warn');
      return;
    }
    ensureTrimOn();
    var t = num(v.currentTime, 0);
    var el = trimRangeEls();
    applyTrimRange(t, num(el.er.value, state.cur.duration), { snapStart: true });
    toast('起点已设为当前播放位置 ' + t.toFixed(1) + 's', 'info', { duration: 2200 });
  }

  function setEndFromPlayer() {
    var v = $('#preview');
    if (!(state.cur.duration > 0) && !(v.duration > 0)) {
      toast('还没有可用的视频，请先载入文件', 'warn');
      return;
    }
    ensureTrimOn();
    var t = num(v.currentTime, 0);
    var el = trimRangeEls();
    applyTrimRange(num(el.sr.value, 0), t, { snapEnd: true });
    toast('终点已设为当前播放位置 ' + t.toFixed(1) + 's', 'info', { duration: 2200 });
  }

  function syncCompressUI() {
    var mode = radioValue('compress-mode') || 'crf';
    var crfBox = $('#crf-box');
    var targetBox = $('#target-box');
    if (crfBox) crfBox.hidden = mode !== 'crf';
    if (targetBox) targetBox.hidden = mode !== 'target';
    var desc = CRF_DESC[radioValue('crf') || '21'] || '';
    $('#crf-desc').textContent = mode === 'crf' ? desc : '当前未使用 CRF 质量模式。';
  }

  function syncTrimModeUI() {
    var mode = radioValue('trim-mode') || 'smart';
    $('#trim-mode-desc').textContent = TRIM_MODE_DESC[mode] || '';
    var el = trimRangeEls();
    if (mode === 'smart') {
      // 切到智能模式时立即吸附一次
      applyTrimRange(num(el.sr.value, 0), num(el.er.value, 0), { snapStart: true, snapEnd: true });
    } else {
      setHint('#trim-snap-hint', '');
    }
  }

  /** 收集 /api/edit 与 /api/estimate 的请求体，顺便做前端校验 */
  function collectEditPayload() {
    var path = String($('#edit-path').value || '').trim();
    if (!path) throw new Error('请先选择或输入要处理的视频文件路径');

    var on = trimEnabled();
    var el = trimRangeEls();
    var start = num(el.si.value, 0);
    var end = num(el.ei.value, 0);
    if (on && !(end > start)) throw new Error('裁剪终点必须大于起点，请检查时间设置');

    var cmode = radioValue('compress-mode') || 'crf';
    var targetMb = num($('#target-mb').value, 0);
    if (cmode === 'target' && !(targetMb > 0)) throw new Error('目标体积模式下请填写大于 0 的目标体积（MB）');

    // 去水印：启用且至少有一个框时才随请求提交（后端对 enabled=false / 空 boxes 会直接忽略）
    var watermark = wmPayload();
    if (!on && cmode === 'none' && !watermark) throw new Error('请至少启用「裁剪」或选择一种压缩方式，否则任务无事可做');

    var useNvenc = $('#use-nvenc').checked && !$('#use-nvenc').disabled;

    var payload = {
      path: path,
      trim: {
        enabled: on,
        start: round1(start),
        end: round1(end),
        mode: radioValue('trim-mode') || 'smart'
      },
      compress: {
        enabled: cmode !== 'none',
        mode: cmode,
        crf: num(radioValue('crf'), 21),
        preset: $('#preset').value || 'medium',
        max_height: num($('#max-height').value, 0),
        target_mb: cmode === 'target' ? targetMb : 0,
        use_nvenc: !!useNvenc,
        mute: $('#mute').checked,
        container: $('#container').value || 'mp4'
      }
    };
    if (watermark) payload.watermark = watermark;
    return payload;
  }

  function renderEstimate(r) {
    var el = $('#estimate-result');
    r = r || {};
    var src = Number(r.src_size) || 0;
    var est = Number(r.est_size) || 0;
    var dur = Number(r.duration) || 0;
    var txt = '预估输出 ' + fmtBytes(est) + '（源 ' + fmtBytes(src);
    if (src > 0 && est > 0) txt += ' · 约 ' + (est / src * 100).toFixed(0) + '%';
    txt += '）';
    if (dur > 0) txt += ' · 输出时长 ' + dur.toFixed(1) + 's';
    if (r.note) txt += ' · ' + r.note;
    el.textContent = txt;
    el.className = 'estimate ' + (src > 0 && est > src ? 'is-warn' : 'is-ok');
  }

  async function doEstimate() {
    var payload;
    try {
      payload = collectEditPayload();
    } catch (e) {
      toast(e.message, 'warn');
      return;
    }
    var btn = $('#btn-estimate');
    setBusy(btn, true, '预估中…');
    var out = $('#estimate-result');
    out.textContent = '正在预估…';
    out.className = 'estimate';
    try {
      var res = await api('/api/estimate', payload);
      renderEstimate(res);
    } catch (e) {
      // 预估属于可选能力，失败不阻塞主流程
      out.textContent = '预估失败：' + e.message;
      out.className = 'estimate is-warn';
      toast('预估失败：' + e.message, 'warn');
    } finally {
      setBusy(btn, false);
    }
  }

  async function doEdit() {
    var payload;
    try {
      payload = collectEditPayload();
    } catch (e) {
      toast(e.message, 'warn');
      return;
    }
    // 去水印是逐像素修复，必须重新编码，无损模式不再可用
    if (payload.watermark) {
      toast('已启用去水印：本次输出会重新编码，无法使用无损模式', 'info', { duration: 4200 });
    }
    var btn = $('#btn-edit');
    setBusy(btn, true, '提交中…');
    try {
      var res = await api('/api/edit', payload);
      var id = res && res.task_id;
      if (id) {
        state.pendingHighlight.add(id);
        upsertTask({
          id: id,
          kind: (payload.compress.enabled || payload.watermark) ? 'compress' : 'trim',
          title: basename(payload.path),
          status: 'pending',
          phase: '已提交，等待开始',
          percent: 0,
          created: Date.now() / 1000
        }, true);
      }
      toast(id ? '已提交处理任务（' + id + '）' : '已提交处理任务', 'success');
      focusTaskPanel();
      refreshTasks();
    } catch (e) {
      toast('提交处理任务失败：' + e.message, 'error');
    } finally {
      setBusy(btn, false);
    }
  }

  async function pickInputFile() {
    var btn = $('#btn-pick-input');
    setBusy(btn, true, '选择中…');
    try {
      var res = await api('/api/import/pick', {});
      var items = Array.isArray(res.items) ? res.items : [];
      if (!items.length) {
        toast('没有选择任何文件', 'info');
        return;
      }
      var item = items[0];
      var path = itemLocalPath(item);
      if (!path) {
        toast('后端没有返回该文件的本地路径，无法载入剪辑', 'error');
        return;
      }
      await loadEditPath(path);
      if (items.length > 1) toast('已载入第 1 个文件，其余 ' + (items.length - 1) + ' 个未载入', 'info');
    } catch (e) {
      toast('调起文件选择框失败：' + e.message, 'error');
    } finally {
      setBusy(btn, false);
    }
  }

  /* ======================================================================
   * 7b. watermark —— 去水印（B站投稿水印等）
   *
   * B站投稿水印在转码时就烧进了画面（所有清晰度都带），只能后期用 delogo 插值 /
   * 模糊 / 马赛克遮盖；水印位置每个视频都不一样，还可能是随时间漂移的。
   * 所以这里做两件事：
   *   1. 在预览帧上按「原始视频像素坐标」框选区域（拖拽画框 / 拖动平移 / 四角缩放）；
   *   2. 给每个框记录生效时间区间 [t0, t1]，移动水印就切成若干限时区域。
   * 数据统一存在 state.wm.boxes，画布与列表都是它的视图。
   * ==================================================================== */

  /** 当前要处理的文件路径（以输入框为准，跟着 state.cur 兜底） */
  function wmSrcPath() {
    var p = String($('#edit-path').value || '').trim();
    return p || state.cur.path || '';
  }

  /** 总开关：未勾选时区块内其他控件一律置灰 */
  function wmEnabled() {
    var cb = $('#wm-enable');
    return !!(cb && cb.checked);
  }

  function wmMode() {
    var m = radioValue('wm-mode');
    return WM_MODES.indexOf(m) >= 0 ? m : 'auto';
  }

  function wmStrength() {
    return clamp(Math.round(num($('#wm-strength').value, 3)), 1, 5);
  }

  /** 视频总时长（优先后端 probe 结果，其次 video 元素） */
  function wmDuration() {
    if (state.cur.duration > 0) return state.cur.duration;
    var v = $('#preview');
    var d = num(v && v.duration, 0);
    return d > 0 ? d : 0;
  }

  /** 原始视频像素尺寸 —— 画布坐标换算的唯一依据 */
  function wmVideoSize() {
    var info = currentMediaInfo();
    var w = Math.round(num(info.width, 0));
    var h = Math.round(num(info.height, 0));
    return (w > 0 && h > 0) ? { w: w, h: h } : { w: 0, h: 0 };
  }

  /** 当前预览帧时间（秒）；时长未知时退化为输入框里的原值 */
  function wmFrameTime() {
    var t = num($('#wm-time').value, 0);
    var d = wmDuration();
    return d > 0 ? clamp(t, 0, d) : Math.max(0, t);
  }

  /** 新框默认的结束时间：0 表示「到视频结束」 */
  function wmBoxEnd() {
    var d = wmDuration();
    return d > 0 ? round1(d) : 0;
  }

  /** 是否为限时区域（不是全程生效） */
  function wmIsTimed(b) {
    if (num(b.t0, 0) > 0.05) return true;
    var t1 = num(b.t1, 0);
    var d = wmDuration();
    return t1 > 0 && d > 0 && t1 < d - 0.05;
  }

  /** /api/frame 的 URL：path 必须 urlencode；boxes 是 JSON 字符串，再 urlencode 一次 */
  function wmFrameUrl(path, t, width, boxes) {
    var url = '/api/frame?path=' + encodeURIComponent(path) +
      '&t=' + round1(clamp(num(t, 0), 0, 86400)) +
      '&w=' + Math.max(64, Math.round(num(width, WM_FRAME_W)));
    if (boxes && boxes.length) url += '&boxes=' + encodeURIComponent(JSON.stringify(boxes));
    return url;
  }

  /**
   * 预览帧用的框数组：把当前模式塞进每个框的 mode，
   * 这样「处理效果预览」和真正提交时的处理方式一致（/api/frame 支持 per-box mode）。
   */
  function wmPreviewBoxes() {
    var mode = wmMode();
    return state.wm.boxes.map(function (b) {
      return {
        x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.w), h: Math.round(b.h),
        t0: round1(b.t0), t1: round1(b.t1), mode: mode
      };
    });
  }

  /**
   * /api/edit 与 /api/estimate 的 watermark 字段。
   * 未启用或没有任何框时返回 null —— 后端遇到 enabled=false / 空 boxes 也会忽略。
   */
  function wmPayload() {
    if (!wmEnabled() || !state.wm.boxes.length) return null;
    return {
      enabled: true,
      mode: wmMode(),
      strength: wmStrength(),
      boxes: state.wm.boxes.map(function (b) {
        return {
          x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.w), h: Math.round(b.h),
          t0: round1(b.t0), t1: round1(b.t1)
        };
      })
    };
  }

  /** 显示坐标 -> 原始视频像素坐标（用归一化比例换算，避免预览帧缩放带来的误差） */
  function wmPointFromEvent(ev) {
    var vs = wmVideoSize();
    var img = $('#wm-frame');
    var rect = (img && img.getBoundingClientRect) ? img.getBoundingClientRect() : null;
    if (!rect || !(vs.w > 0)) return { x: 0, y: 0 };
    var dw = rect.width || 1;
    var dh = rect.height || 1;
    return {
      x: clamp((num(ev.clientX, 0) - rect.left) / dw, 0, 1) * vs.w,
      y: clamp((num(ev.clientY, 0) - rect.top) / dh, 0, 1) * vs.h
    };
  }

  /** 能否在画布上操作（启用 + 有文件 + 已知分辨率） */
  function wmCanEdit() {
    return !!(wmEnabled() && wmSrcPath() && wmVideoSize().w > 0);
  }

  /* ---------------------------- 画布提示 ---------------------------- */

  function wmUpdateHint() {
    var el = $('#wm-canvas-hint');
    if (!el) return;
    var text = '';
    var cls = 'wm-canvas-hint';
    if (!wmSrcPath()) {
      text = '请先在上方「输入文件」中选择或载入视频';
    } else if (!wmEnabled()) {
      text = '勾选「启用去水印」后即可在画面上拖拽框选';
    } else if (!(wmVideoSize().w > 0)) {
      text = '正在读取视频信息…若长时间没有反应，请确认文件可以正常播放';
    } else if (state.wm.frameErr) {
      text = '预览帧加载失败：请确认后端 /api/frame 接口可用';
      cls += ' is-err';
    } else if (state.wm.loading) {
      text = '正在加载预览帧…';
    } else if (!state.wm.boxes.length) {
      text = '在画面上按住鼠标左键拖拽，即可框出水印区域';
    }
    el.textContent = text;
    el.className = cls;
    el.hidden = !text;
  }

  /* ---------------------------- 画布渲染 ---------------------------- */

  function wmRenderCanvas() {
    var layer = $('#wm-layer');
    if (!layer) return;
    layer.textContent = '';
    var vs = wmVideoSize();
    if (vs.w > 0 && vs.h > 0) {
      var drag = state.wm.drag;
      state.wm.boxes.forEach(function (b, i) {
        layer.appendChild(wmBuildBoxEl(b, i, vs, drag));
      });
    }
    wmUpdateHint();
  }

  function wmBuildBoxEl(b, i, vs, drag) {
    var pct = function (v, total) { return (clamp(num(v, 0) / total, 0, 1) * 100).toFixed(4) + '%'; };
    var draft = !!(drag && drag.idx === i);
    var el = h('div', {
      class: 'wm-box' + (i === state.wm.sel ? ' is-sel' : '') + (draft ? ' is-draft' : ''),
      dataset: { idx: String(i) },
      title: '区域 ' + (i + 1) + '：X' + Math.round(b.x) + ' Y' + Math.round(b.y) +
        ' 宽' + Math.round(b.w) + ' 高' + Math.round(b.h),
      style: 'left:' + pct(b.x, vs.w) + ';top:' + pct(b.y, vs.h) + ';' +
        'width:' + pct(b.w, vs.w) + ';height:' + pct(b.h, vs.h) + ';'
    });
    el.appendChild(h('span', { class: 'wm-box-no', text: String(i + 1) }));
    if (wmIsTimed(b)) {
      el.appendChild(h('span', {
        class: 'wm-box-tag',
        text: round1(b.t0).toFixed(1) + '~' + round1(b.t1).toFixed(1) + 's'
      }));
    }
    WM_HANDLES.forEach(function (pos) {
      el.appendChild(h('span', { class: 'wm-handle wm-handle-' + pos, dataset: { handle: pos } }));
    });
    var del = h('button', { class: 'wm-box-del', type: 'button', title: '删除该区域', text: '✕' });
    del.addEventListener('click', function (ev) {
      if (ev.stopPropagation) ev.stopPropagation();
      if (ev.preventDefault) ev.preventDefault();
      wmRemoveBox(i);
    });
    el.appendChild(del);
    return el;
  }

  /* ---------------------------- 框列表 ---------------------------- */

  function wmRowInput(i, key) {
    var tbody = $('#wm-list');
    var row = (tbody && tbody.children) ? tbody.children[i] : null;
    if (!row || !row.querySelector) return null;
    return row.querySelector('input[data-k="' + key + '"]');
  }

  /** 把 state 里的值写回某个列表行（force=false 时跳过正在输入的控件，避免光标跳动） */
  function wmWriteInput(i, key, value, force) {
    var inp = wmRowInput(i, key);
    if (!inp) return;
    if (!force && inp === document.activeElement) return;
    inp.value = (key === 't0' || key === 't1')
      ? round1(value).toFixed(1)
      : String(Math.round(num(value, 0)));
  }

  /** 画布 -> 列表：拖拽 / 检测后把最新坐标写回输入框 */
  function wmUpdateRowValues(i) {
    var b = state.wm.boxes[i];
    if (!b) return;
    ['x', 'y', 'w', 'h', 't0', 't1'].forEach(function (k) { wmWriteInput(i, k, b[k], false); });
    wmRefreshLimits(i);
  }

  /** 根据当前框刷新同行输入框的可用范围 */
  function wmRefreshLimits(i) {
    var b = state.wm.boxes[i];
    if (!b) return;
    var vs = wmVideoSize();
    var limits = {
      x: [0, Math.max(0, vs.w - b.w)],
      y: [0, Math.max(0, vs.h - b.h)],
      w: [WM_MIN_SIZE, Math.max(WM_MIN_SIZE, vs.w - b.x)],
      h: [WM_MIN_SIZE, Math.max(WM_MIN_SIZE, vs.h - b.y)]
    };
    Object.keys(limits).forEach(function (k) {
      var inp = wmRowInput(i, k);
      if (!inp) return;
      inp.min = limits[k][0];
      inp.max = limits[k][1];
    });
    var d = wmDuration();
    if (d > 0) {
      ['t0', 't1'].forEach(function (k) {
        var inp = wmRowInput(i, k);
        if (!inp) return;
        inp.min = 0;
        inp.max = d;
      });
    }
  }

  function wmRenderList() {
    var tbody = $('#wm-list');
    if (!tbody) return;
    tbody.textContent = '';
    var boxes = state.wm.boxes;
    if (!boxes.length) {
      tbody.appendChild(h('tr', { class: 'wm-empty-row' },
        h('td', {
          colspan: '8',
          text: '还没有区域：在画布上按住鼠标左键拖拽框选，或点击「自动检测水印」自动识别（B站投稿水印通常是右上 / 右下角的昵称 + logo）'
        })
      ));
      wmUpdateStat();
      return;
    }
    var vs = wmVideoSize();
    var dur = wmDuration();
    boxes.forEach(function (b, i) { tbody.appendChild(wmBuildRow(b, i, vs, dur)); });
    wmUpdateStat();
  }

  function wmBuildRow(b, i, vs, dur) {
    var fields = [
      { k: 'x', v: Math.round(b.x), min: 0, max: Math.max(0, vs.w - b.w), step: 1, tip: 'X 左边距' },
      { k: 'y', v: Math.round(b.y), min: 0, max: Math.max(0, vs.h - b.h), step: 1, tip: 'Y 上边距' },
      { k: 'w', v: Math.round(b.w), min: WM_MIN_SIZE, max: Math.max(WM_MIN_SIZE, vs.w - b.x), step: 1, tip: '宽度' },
      { k: 'h', v: Math.round(b.h), min: WM_MIN_SIZE, max: Math.max(WM_MIN_SIZE, vs.h - b.y), step: 1, tip: '高度' },
      { k: 't0', v: round1(b.t0).toFixed(1), min: 0, max: dur > 0 ? dur : '', step: 0.1, tip: '起始秒' },
      { k: 't1', v: round1(b.t1).toFixed(1), min: 0, max: dur > 0 ? dur : '', step: 0.1, tip: '结束秒' }
    ];

    var row = h('tr', { dataset: { idx: String(i) } });
    row.appendChild(h('td', { class: 'wm-td-no' },
      h('span', { class: 'wm-no-badge' + (i === state.wm.sel ? ' is-sel' : ''), text: String(i + 1) })
    ));
    fields.forEach(function (f) {
      var input = h('input', {
        type: 'number', class: 'input wm-num', value: f.v, step: f.step,
        min: f.min, max: f.max, title: f.tip + '（原始像素 / 秒）',
        dataset: { k: f.k }, 'aria-label': '区域 ' + (i + 1) + ' 的' + f.tip
      });
      input.addEventListener('input', function () { wmApplyNumber(i, f.k, input, false); });
      input.addEventListener('change', function () { wmApplyNumber(i, f.k, input, true); });
      row.appendChild(h('td', null, input));
    });
    var del = h('button', { class: 'btn btn-ghost btn-sm wm-row-del', type: 'button', title: '删除该区域', text: '✕' });
    del.addEventListener('click', function () { wmRemoveBox(i); });
    row.appendChild(h('td', { class: 'wm-td-act' }, del));

    if (i === state.wm.sel) row.classList.add('is-sel');
    row.addEventListener('click', function (ev) {
      var tg = ev.target;
      var tag = (tg && tg.tagName) ? String(tg.tagName).toLowerCase() : '';
      if (tag === 'input' || tag === 'button') return;
      wmSelect(i);
    });
    return row;
  }

  /** 列表输入框 -> state（input 事件实时同步；change 事件额外把归一化后的值写回输入框） */
  function wmApplyNumber(i, key, input, normalize) {
    var b = state.wm.boxes[i];
    if (!b) return;
    var vs = wmVideoSize();
    var dur = wmDuration();
    var v = num(input.value, b[key]);
    if (key === 'x') {
      b.x = clamp(Math.round(v), 0, Math.max(0, vs.w - b.w));
    } else if (key === 'y') {
      b.y = clamp(Math.round(v), 0, Math.max(0, vs.h - b.h));
    } else if (key === 'w') {
      b.w = clamp(Math.round(v), WM_MIN_SIZE, Math.max(WM_MIN_SIZE, vs.w - b.x));
    } else if (key === 'h') {
      b.h = clamp(Math.round(v), WM_MIN_SIZE, Math.max(WM_MIN_SIZE, vs.h - b.y));
    } else if (key === 't0') {
      b.t0 = round1(Math.max(0, dur > 0 ? clamp(v, 0, dur) : v));
      if (b.t1 > 0 && b.t0 > b.t1) b.t1 = b.t0;
      wmWriteInput(i, 't1', b.t1, false);
    } else if (key === 't1') {
      b.t1 = round1(Math.max(0, dur > 0 ? clamp(v, 0, dur) : v));
      if (b.t1 > 0 && b.t1 < b.t0) b.t0 = b.t1;
      wmWriteInput(i, 't0', b.t0, false);
    }
    if (normalize) wmWriteInput(i, key, b[key], true);
    wmRefreshLimits(i);
    wmRenderCanvas();
    wmUpdateStat();
  }

  /* ---------------------------- 统计与状态 ---------------------------- */

  function wmUpdateStat() {
    var n = state.wm.boxes.length;
    var timed = 0;
    state.wm.boxes.forEach(function (b) { if (wmIsTimed(b)) timed++; });

    var stat = $('#wm-stat');
    if (stat) stat.textContent = '共 ' + n + ' 个区域（其中 ' + timed + ' 个为限时区域）';

    var pill = $('#wm-count-pill');
    if (pill) {
      var on = wmEnabled() && n > 0;
      pill.textContent = on ? ('已启用 · ' + n + ' 个区域') : (n ? (n + ' 个区域 · 未启用') : '未启用');
      pill.className = 'pill' + (on ? ' pill-ok' : (n ? ' pill-warn' : ''));
    }

    // 「删除选中」「清空」的可用性由这里统一决定（wmSyncUI 不插手，避免互相覆盖）
    var del = $('#wm-del');
    if (del) del.disabled = !(wmEnabled() && n > 0 && state.wm.sel >= 0 && state.wm.sel < n);
    var clr = $('#wm-clear');
    if (clr) clr.disabled = !(wmEnabled() && n > 0);
  }

  /** 选中高亮（画布 + 列表），不重建列表 */
  function wmSelect(i) {
    state.wm.sel = (i >= 0 && i < state.wm.boxes.length) ? i : -1;
    wmRenderCanvas();
    var tbody = $('#wm-list');
    if (tbody) {
      Array.prototype.forEach.call(tbody.children, function (row, idx) {
        var on = idx === state.wm.sel;
        row.classList.toggle('is-sel', on);
        var badge = row.querySelector ? row.querySelector('.wm-no-badge') : null;
        if (badge) badge.classList.toggle('is-sel', on);
      });
    }
    wmUpdateStat();
  }

  /** 总开关 / 模式 / 时长变化后刷新整个区块的可用状态 */
  function wmSyncUI() {
    var path = wmSrcPath();
    var hasFile = !!path;
    var on = hasFile && wmEnabled();
    var mode = wmMode();
    var dur = wmDuration();

    var panel = $('#wm-panel');
    if (panel) panel.classList.toggle('is-locked', !hasFile);

    var lock = $('#wm-lock');
    if (lock) {
      lock.hidden = hasFile;
      if (!hasFile) {
        lock.textContent = '还没有选择视频文件：请先在上方「输入文件」中载入文件，去水印需要按原始像素坐标框选区域。';
      }
    }

    var hint = $('#wm-summary-hint');
    if (hint) {
      hint.textContent = !hasFile
        ? '水印位置因视频而异，可能随时间漂移'
        : (wmEnabled() ? '拖拽框选 / 自动检测，坐标按原始像素记录' : '勾选「启用去水印」后可用');
    }

    var cb = $('#wm-enable');
    if (cb) cb.disabled = !hasFile;

    var enableHint = $('#wm-enable-hint');
    if (enableHint) {
      enableHint.textContent = !hasFile
        ? '载入文件后可用'
        : (wmEnabled()
          ? '已启用：本次输出必须重新编码，无法使用无损模式'
          : '勾选后即可框选 / 检测区域；未勾选时区块内的其他控件不可用');
    }

    $$('#wm-mode input[name="wm-mode"]').forEach(function (r) { r.disabled = !on; });
    var desc = $('#wm-mode-desc');
    if (desc) desc.textContent = WM_MODE_DESC[mode] || '';

    var strengthOn = on && (mode === 'blur' || mode === 'mosaic');
    var strength = $('#wm-strength');
    if (strength) strength.disabled = !strengthOn;
    var sval = $('#wm-strength-val');
    if (sval) sval.textContent = String(wmStrength());
    setHint('#wm-strength-hint',
      strengthOn ? '强度越高遮盖越彻底，画面痕迹也越明显' : '仅「模糊 / 马赛克」模式可调强度，其余模式自动决定处理方式',
      strengthOn ? '' : 'hint-warn');

    ['#wm-now', '#wm-detect', '#wm-add', '#wm-compare'].forEach(function (sel) {
      var el = $(sel);
      if (el) el.disabled = !on;
    });
    var detect = $('#wm-detect');
    if (detect && state.wm.detecting) detect.disabled = true;

    var time = $('#wm-time');
    if (time) {
      time.max = dur > 0 ? dur : 1;
      if (!(dur > 0)) time.value = 0;
      time.disabled = !(on && dur > 0);
    }
    var label = $('#wm-time-label');
    if (label) {
      var t = wmFrameTime();
      label.textContent = dur > 0
        ? (t.toFixed(1) + 's / ' + dur.toFixed(1) + 's')
        : '时长未知（框默认全程生效）';
    }

    wmUpdateStat();
    wmRenderCanvas();
    wmUpdateHint();
  }

  /* ---------------------------- 取预览帧 ---------------------------- */

  /** 取一帧预览图；immediate=false 时做防抖（拖动时间滑块时用） */
  function wmRequestFrame(immediate) {
    var img = $('#wm-frame');
    if (!img) return;
    clearTimeout(state.wm.timer);
    state.wm.timer = null;
    var run = function () {
      state.wm.timer = null;
      var path = wmSrcPath();
      if (!path) {
        img.removeAttribute('src');
        state.wm.loading = false;
        wmUpdateHint();
        return;
      }
      var vs = wmVideoSize();
      var w = vs.w > 0 ? Math.min(WM_FRAME_W, vs.w) : WM_FRAME_W;
      state.wm.loading = true;
      state.wm.frameErr = false;
      wmUpdateHint();
      img.src = wmFrameUrl(path, wmFrameTime(), w, null);
    };
    if (immediate) run();
    else state.wm.timer = setTimeout(run, WM_FRAME_DELAY);
  }

  /** 参数变化后刷新「处理后」对比图（仅在对比预览已展开时） */
  function wmRefreshCompare() {
    var box = $('#wm-compare-box');
    if (!box || box.hidden) return;
    if (!state.wm.boxes.length) { box.hidden = true; return; }
    wmCompare();
  }

  /* ---------------------------- 交互动作 ---------------------------- */

  function wmNormalizeBox(raw, duration) {
    raw = raw || {};
    var vs = wmVideoSize();
    var w = Math.max(WM_MIN_SIZE, Math.round(num(raw.w, WM_ADD_W)));
    var hgt = Math.max(WM_MIN_SIZE, Math.round(num(raw.h, WM_ADD_H)));
    var x = Math.max(0, Math.round(num(raw.x, 0)));
    var y = Math.max(0, Math.round(num(raw.y, 0)));
    if (vs.w > 0) { x = Math.min(x, Math.max(0, vs.w - WM_MIN_SIZE)); w = Math.min(w, vs.w - x); }
    if (vs.h > 0) { y = Math.min(y, Math.max(0, vs.h - WM_MIN_SIZE)); hgt = Math.min(hgt, vs.h - y); }
    var d = duration > 0 ? duration : wmDuration();
    var t0 = Math.max(0, round1(num(raw.t0, 0)));
    var t1 = round1(num(raw.t1, 0));
    // 后端用 t1=0 表示「到视频结束」，界面上归一成明确的秒数，便于直接编辑
    if (!(t1 > 0)) t1 = d > 0 ? round1(d) : 0;
    if (d > 0) { t0 = Math.min(t0, d); t1 = Math.min(t1, d); }
    if (t1 > 0 && t1 < t0) t1 = t0;
    return {
      x: x, y: y,
      w: Math.max(WM_MIN_SIZE, w), h: Math.max(WM_MIN_SIZE, hgt),
      t0: t0, t1: t1
    };
  }

  function wmRemoveBox(i) {
    if (!(i >= 0 && i < state.wm.boxes.length)) return;
    state.wm.boxes.splice(i, 1);
    if (state.wm.sel === i) state.wm.sel = -1;
    else if (state.wm.sel > i) state.wm.sel -= 1;
    wmRenderCanvas();
    wmRenderList();
    toast('已删除区域 ' + (i + 1), 'info', { duration: 1800 });
  }

  function wmAddBox() {
    var vs = wmVideoSize();
    if (!(vs.w > 0)) {
      toast('尚未读取到视频分辨率，暂时无法添加区域', 'warn');
      return;
    }
    var w = Math.min(WM_ADD_W, vs.w);
    var hgt = Math.min(WM_ADD_H, vs.h);
    state.wm.boxes.push({
      x: clamp(Math.round((vs.w - w) / 2), 0, Math.max(0, vs.w - w)),
      y: clamp(Math.round((vs.h - hgt) / 2), 0, Math.max(0, vs.h - hgt)),
      w: w, h: hgt, t0: 0, t1: wmBoxEnd()
    });
    state.wm.sel = state.wm.boxes.length - 1;
    wmRenderCanvas();
    wmRenderList();
    toast('已在画面中央添加一个 ' + w + '×' + hgt + ' 的区域，可拖动 / 缩放调整', 'info', { duration: 2600 });
  }

  function wmDeleteSelected() {
    if (!(state.wm.sel >= 0 && state.wm.boxes[state.wm.sel])) {
      toast('请先在画布或列表里点选一个区域', 'warn');
      return;
    }
    wmRemoveBox(state.wm.sel);
  }

  function wmClearBoxes() {
    var n = state.wm.boxes.length;
    if (!n) {
      toast('当前没有可清空的区域', 'info');
      return;
    }
    state.wm.boxes = [];
    state.wm.sel = -1;
    var box = $('#wm-compare-box');
    if (box) box.hidden = true;
    setHint('#wm-compare-status', '');
    wmRenderCanvas();
    wmRenderList();
    toast('已清空 ' + n + ' 个区域', 'success');
  }

  function wmSetTimeFromPlayer() {
    var v = $('#preview');
    var t = num(v && v.currentTime, 0);
    var d = wmDuration();
    if (!(d > 0) && !(t > 0)) {
      toast('还没有可用的视频，请先载入文件', 'warn');
      return;
    }
    var time = $('#wm-time');
    if (time) time.value = d > 0 ? clamp(t, 0, d) : Math.max(0, t);
    wmSyncUI();
    wmRequestFrame(true);
    toast('预览帧已定位到当前播放位置 ' + t.toFixed(1) + 's', 'info', { duration: 2200 });
  }

  /** 对比预览：左原始 / 右按当前参数处理后的效果 */
  function wmCompare() {
    var path = wmSrcPath();
    if (!path) {
      toast('请先选择或载入视频文件', 'warn');
      return;
    }
    if (!state.wm.boxes.length) {
      toast('请先添加或检测水印区域', 'warn');
      return;
    }
    var box = $('#wm-compare-box');
    var src = $('#wm-cmp-src');
    var out = $('#wm-cmp-out');
    if (!box || !src || !out) return;
    var t = wmFrameTime();
    box.hidden = false;
    src.src = wmFrameUrl(path, t, WM_CMP_W, null);
    out.src = wmFrameUrl(path, t, WM_CMP_W, wmPreviewBoxes());
    setHint('#wm-compare-status',
      '第 ' + t.toFixed(1) + 's 帧：左为原始画面，右为按当前参数处理后的效果（w=' + WM_CMP_W + '）',
      'hint-accent');
  }

  async function wmDetect() {
    var path = wmSrcPath();
    if (!path) {
      toast('请先选择或载入视频文件', 'warn');
      return;
    }
    if (state.wm.detecting) return;
    state.wm.detecting = true;
    var btn = $('#wm-detect');
    setBusy(btn, true, '检测中…');
    setHint('#wm-detect-status', '正在分析视频时域特征…', 'hint-accent');
    try {
      var res = await api('/api/watermark/detect', { path: path, samples: 48 });
      if (!res || res.ok === false) {
        var why = (res && res.note) || '后端没有返回检测结果';
        setHint('#wm-detect-status', '检测失败：' + why, 'hint-warn');
        toast('自动检测失败：' + why, 'error', { duration: 6000 });
        return;
      }
      var raw = Array.isArray(res.boxes) ? res.boxes.slice() : [];
      if (!raw.length && (Array.isArray(res.static_boxes) || Array.isArray(res.segments))) {
        // 兜底：后端只给了 static_boxes / segments 时自己拼一份
        raw = (res.static_boxes || []).concat(res.segments || []);
      }
      var dur = num(res.duration, 0) || wmDuration();
      state.wm.boxes = raw.map(function (r) { return wmNormalizeBox(r, dur); });
      state.wm.sel = state.wm.boxes.length ? 0 : -1;
      wmRenderCanvas();
      wmRenderList();
      var note = res.note || '';
      if (!state.wm.boxes.length) {
        setHint('#wm-detect-status', '未检测到明显水印，可手动框选', 'hint-warn');
        toast('未检测到明显水印，可手动框选', 'warn', { duration: 6000 });
      } else {
        setHint('#wm-detect-status', note || ('检测到 ' + state.wm.boxes.length + ' 处区域'), 'hint-accent');
        toast(note || ('检测到 ' + state.wm.boxes.length + ' 处水印区域'), 'success', { duration: 6000 });
        // 移动水印：把预览定位到第一段区域的起始时间，方便直接核对
        if (num(state.wm.boxes[0].t0, 0) > 0 && wmDuration() > 0) {
          var time = $('#wm-time');
          if (time) time.value = clamp(state.wm.boxes[0].t0, 0, wmDuration());
          wmRequestFrame(true);
        }
      }
      wmRefreshCompare();
    } catch (e) {
      setHint('#wm-detect-status', '检测失败：' + e.message, 'hint-warn');
      toast('自动检测水印失败：' + e.message, 'error', { duration: 6000 });
    } finally {
      state.wm.detecting = false;
      setBusy(btn, false);
      wmSyncUI();
    }
  }

  /* ---------------------------- 画布拖拽（pointer 事件） ---------------------------- */

  function wmOnPointerDown(ev) {
    if (ev.button !== undefined && ev.button !== null && ev.button !== 0) return;
    if (!wmCanEdit()) {
      if (wmEnabled() && wmSrcPath() && !(wmVideoSize().w > 0)) {
        toast('尚未读取到视频分辨率，暂时无法框选区域', 'warn');
      }
      return;
    }
    var target = ev.target;
    if (target && target.closest && target.closest('.wm-box-del')) return;  // 删除按钮自己处理 click
    var handleEl = (target && target.closest) ? target.closest('.wm-handle') : null;
    var boxEl = (target && target.closest) ? target.closest('.wm-box') : null;
    var start = wmPointFromEvent(ev);
    var idx = boxEl ? num(boxEl.dataset.idx, -1) : -1;
    var mode = boxEl ? (handleEl ? 'resize' : 'move') : 'draw';

    if (mode === 'draw') {
      // 按下即建框，移动时实时改尺寸；松手时若没拖动就撤掉
      state.wm.boxes.push({
        x: Math.round(start.x), y: Math.round(start.y),
        w: 0, h: 0, t0: 0, t1: wmBoxEnd()
      });
      idx = state.wm.boxes.length - 1;
    }
    var b = state.wm.boxes[idx];
    if (!b) return;

    state.wm.sel = idx;
    state.wm.drag = {
      mode: mode, idx: idx,
      handle: handleEl ? String(handleEl.dataset.handle || '') : '',
      start: start,
      orig: { x: num(b.x, 0), y: num(b.y, 0), w: num(b.w, 0), h: num(b.h, 0) }
    };

    // 指针捕获挂在外层容器上：拖出画布（甚至拖出窗口）也不会丢事件
    var canvas = $('#wm-canvas');
    if (canvas && canvas.setPointerCapture) {
      try { canvas.setPointerCapture(ev.pointerId); } catch (e) { /* 忽略 */ }
    }
    if (ev.preventDefault) ev.preventDefault();
    wmRenderCanvas();
    wmRenderList();
  }

  function wmOnPointerMove(ev) {
    var drag = state.wm.drag;
    if (!drag) return;
    var b = state.wm.boxes[drag.idx];
    if (!b) { state.wm.drag = null; return; }
    var vs = wmVideoSize();
    if (!(vs.w > 0)) return;
    var p = wmPointFromEvent(ev);

    if (drag.mode === 'move') {
      b.x = clamp(Math.round(drag.orig.x + (p.x - drag.start.x)), 0, Math.max(0, vs.w - b.w));
      b.y = clamp(Math.round(drag.orig.y + (p.y - drag.start.y)), 0, Math.max(0, vs.h - b.h));
    } else if (drag.mode === 'resize') {
      var l = drag.orig.x, t = drag.orig.y;
      var r = drag.orig.x + drag.orig.w, bo = drag.orig.y + drag.orig.h;
      if (drag.handle === 'nw') { l = clamp(p.x, 0, r - WM_MIN_SIZE); t = clamp(p.y, 0, bo - WM_MIN_SIZE); }
      else if (drag.handle === 'ne') { r = clamp(p.x, l + WM_MIN_SIZE, vs.w); t = clamp(p.y, 0, bo - WM_MIN_SIZE); }
      else if (drag.handle === 'sw') { l = clamp(p.x, 0, r - WM_MIN_SIZE); bo = clamp(p.y, t + WM_MIN_SIZE, vs.h); }
      else { r = clamp(p.x, l + WM_MIN_SIZE, vs.w); bo = clamp(p.y, t + WM_MIN_SIZE, vs.h); }
      b.x = Math.round(l); b.y = Math.round(t);
      b.w = Math.round(r - l); b.h = Math.round(bo - t);
    } else {
      var x0 = clamp(drag.start.x, 0, vs.w), y0 = clamp(drag.start.y, 0, vs.h);
      var x1 = clamp(p.x, 0, vs.w), y1 = clamp(p.y, 0, vs.h);
      b.x = Math.round(Math.min(x0, x1));
      b.y = Math.round(Math.min(y0, y1));
      b.w = Math.max(WM_MIN_SIZE, Math.round(Math.abs(x1 - x0)));
      b.h = Math.max(WM_MIN_SIZE, Math.round(Math.abs(y1 - y0)));
      b.w = Math.max(WM_MIN_SIZE, Math.min(b.w, vs.w - b.x));
      b.h = Math.max(WM_MIN_SIZE, Math.min(b.h, vs.h - b.y));
    }

    if (ev.preventDefault) ev.preventDefault();
    wmRenderCanvas();
    wmUpdateRowValues(drag.idx);
    wmUpdateStat();
  }

  function wmOnPointerUp(ev) {
    var drag = state.wm.drag;
    if (!drag) return;
    state.wm.drag = null;
    var canvas = $('#wm-canvas');
    if (canvas && canvas.releasePointerCapture) {
      try { canvas.releasePointerCapture(ev.pointerId); } catch (e) { /* 忽略 */ }
    }
    var b = state.wm.boxes[drag.idx];
    if (!b) { wmRenderCanvas(); wmRenderList(); return; }
    // 只是在画布上点了一下（没拖出面积）-> 不产生新框
    if (drag.mode === 'draw' && b.w <= WM_MIN_SIZE && b.h <= WM_MIN_SIZE) {
      state.wm.boxes.splice(drag.idx, 1);
      state.wm.sel = -1;
      wmRenderCanvas();
      wmRenderList();
      return;
    }
    b.x = Math.round(b.x); b.y = Math.round(b.y);
    b.w = Math.round(b.w); b.h = Math.round(b.h);
    wmRenderCanvas();
    wmRenderList();
    if (drag.mode === 'draw') {
      toast('已添加区域 ' + (drag.idx + 1) +
        '（X' + b.x + ' Y' + b.y + ' ' + b.w + '×' + b.h + '）', 'info', { duration: 2600 });
    }
  }

  /** 换文件：旧坐标不再适用，直接清空 + 重新取帧 */
  function wmOnSourceChanged(path) {
    var had = state.wm.boxes.length;
    state.wm.boxes = [];
    state.wm.sel = -1;
    state.wm.drag = null;
    state.wm.loading = false;
    state.wm.frameErr = false;
    state.wm.errNotified = false;
    clearTimeout(state.wm.timer);
    state.wm.timer = null;
    var time = $('#wm-time');
    if (time) time.value = 0;
    var box = $('#wm-compare-box');
    if (box) box.hidden = true;
    setHint('#wm-detect-status', '');
    setHint('#wm-compare-status', '');
    wmRenderList();
    wmSyncUI();
    if (path) wmRequestFrame(true);
    if (had) toast('已切换文件，之前的 ' + had + ' 个去水印区域已清空', 'info', { duration: 3600 });
  }

  function bindWatermark() {
    $('#wm-enable').addEventListener('change', function () {
      var on = wmEnabled();
      if (on && !(wmVideoSize().w > 0)) {
        toast('尚未读取到视频分辨率，框选会在预览就绪后可用', 'warn');
      }
      if (on && !(wmDuration() > 0)) {
        toast('未读取到视频时长：时间滑块不可用，区域将默认对全片生效', 'warn');
      }
      wmSyncUI();
      if (on) wmRequestFrame(true);
    });

    var time = $('#wm-time');
    time.addEventListener('input', function () {
      wmSyncUI();
      wmRequestFrame(false);
    });
    time.addEventListener('change', function () {
      wmSyncUI();
      wmRequestFrame(true);
    });

    $('#wm-now').addEventListener('click', wmSetTimeFromPlayer);
    $('#wm-detect').addEventListener('click', wmDetect);
    $('#wm-add').addEventListener('click', wmAddBox);
    $('#wm-del').addEventListener('click', wmDeleteSelected);
    $('#wm-clear').addEventListener('click', wmClearBoxes);
    $('#wm-compare').addEventListener('click', wmCompare);

    $$('#wm-mode input[name="wm-mode"]').forEach(function (r) {
      r.addEventListener('change', function () {
        wmSyncUI();
        wmRefreshCompare();
      });
    });

    var strength = $('#wm-strength');
    var showStrength = function () {
      var v = $('#wm-strength-val');
      if (v) v.textContent = String(wmStrength());
    };
    strength.addEventListener('input', showStrength);
    strength.addEventListener('change', function () {
      showStrength();
      wmRefreshCompare();
    });

    var canvas = $('#wm-canvas');
    canvas.addEventListener('pointerdown', wmOnPointerDown);
    canvas.addEventListener('pointermove', wmOnPointerMove);
    canvas.addEventListener('pointerup', wmOnPointerUp);
    canvas.addEventListener('pointercancel', wmOnPointerUp);

    var img = $('#wm-frame');
    img.addEventListener('load', function () {
      state.wm.loading = false;
      state.wm.frameErr = false;
      wmRenderCanvas();
      wmUpdateHint();
    });
    img.addEventListener('error', function () {
      state.wm.loading = false;
      if (!wmSrcPath()) return;
      state.wm.frameErr = true;
      wmUpdateHint();
      if (!state.wm.errNotified) {
        state.wm.errNotified = true;
        toast('预览帧加载失败：请确认后端 /api/frame 接口可用，或换一个时间点重试', 'error', { duration: 6000 });
      }
    });

    [$('#wm-cmp-src'), $('#wm-cmp-out')].forEach(function (el) {
      if (!el) return;
      el.addEventListener('error', function () {
        setHint('#wm-compare-status', '对比预览帧加载失败：请确认后端 /api/frame 接口可用', 'hint-warn');
        toast('对比预览帧加载失败：请确认后端 /api/frame 接口可用', 'error', { duration: 6000 });
      });
    });
  }

  /* ======================================================================
   * 8. tasks —— 底部任务面板 + SSE
   * ==================================================================== */

  function taskResultPath(task) {
    var r = task && task.result;
    if (!r) return '';
    if (typeof r === 'string') return r;
    return r.path || r.output || r.file || '';
  }

  function buildTaskRow(task) {
    var id = task.id;
    var title = h('div', { class: 'task-title', text: task.title || '(未命名任务)' });
    var kindPill = h('span', { class: 'pill pill-accent', text: TASK_KIND_LABEL[task.kind] || task.kind || '任务' });
    var statusPill = h('span', { class: 'pill' });
    var phase = h('span', { class: 'task-phase' });
    var speed = h('span', { class: 'task-speed' });
    var eta = h('span', { class: 'task-eta' });
    var msg = h('span', { class: 'task-msg' });
    var bar = h('div', { class: 'bar' }, h('div', { class: 'bar-fill' }));
    var errBox = h('div', { class: 'task-error', hidden: true });
    var pathEl = h('span', { class: 'task-path' });
    var btnPlay = h('button', { class: 'btn btn-sm', type: 'button', text: '播放' });
    var btnReveal = h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '打开文件夹' });
    var resultBox = h('div', { class: 'task-result', hidden: true }, pathEl, btnPlay, btnReveal);

    var main = h('div', { class: 'task-main' },
      h('div', { class: 'task-line1' }, title, kindPill, statusPill),
      h('div', { class: 'task-line2' }, phase, speed, eta, msg),
      bar, errBox, resultBox
    );

    var pct = h('div', { class: 'task-pct', text: '0%' });
    var btnCancel = h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '取消' });
    var btnRemove = h('button', { class: 'icon-btn', type: 'button', title: '从列表移除', text: '✕' });
    var right = h('div', { class: 'task-right' }, pct, h('div', { class: 'task-actions' }, btnCancel, btnRemove));
    var row = h('div', { class: 'task-row', dataset: { id: id } }, main, right);

    btnCancel.addEventListener('click', function () { cancelTask(id); });
    btnRemove.addEventListener('click', function () { clearTask(id); });
    btnPlay.addEventListener('click', function () {
      var t = state.tasks.get(id);
      var p = taskResultPath(t || task);
      if (!p) { toast('该任务没有可用的输出文件', 'warn'); return; }
      api('/api/open', { path: p })
        .then(function () { toast('已用系统默认播放器打开', 'success'); })
        .catch(function (e) { toast('播放失败：' + e.message, 'error'); });
    });
    btnReveal.addEventListener('click', function () {
      var t = state.tasks.get(id);
      var p = taskResultPath(t || task);
      if (!p) { toast('该任务没有可用的输出文件', 'warn'); return; }
      api('/api/reveal', { path: p })
        .then(function () { toast('已在资源管理器中定位文件', 'success'); })
        .catch(function (e) { toast('打开文件夹失败：' + e.message, 'error'); });
    });

    return {
      row: row, title: title, kindPill: kindPill, statusPill: statusPill,
      phase: phase, speed: speed, eta: eta, msg: msg,
      bar: bar, barFill: bar.querySelector('.bar-fill'),
      errBox: errBox, resultBox: resultBox, pathEl: pathEl,
      pct: pct, btnCancel: btnCancel, btnRemove: btnRemove
    };
  }

  function updateTaskRow(node, task) {
    var status = String(task.status || 'running');
    if (['pending', 'running', 'done', 'error', 'canceled'].indexOf(status) < 0) status = 'running';

    node.row.classList.remove('is-pending', 'is-running', 'is-done', 'is-error', 'is-canceled');
    node.row.classList.add('is-' + status);

    node.title.textContent = task.title || '(未命名任务)';
    node.kindPill.textContent = TASK_KIND_LABEL[task.kind] || task.kind || '任务';
    node.statusPill.textContent = (status === 'done' ? '✓ ' : (status === 'error' ? '✕ ' : '')) +
      (TASK_STATUS_LABEL[status] || status);
    node.statusPill.className = 'pill ' + (
      status === 'done' ? 'pill-ok' :
      status === 'error' ? 'pill-danger' :
      status === 'running' ? 'pill-accent' :
      status === 'canceled' ? '' : 'pill-warn'
    );

    node.phase.textContent = task.phase || TASK_STATUS_DEFAULT_PHASE[status] || '';

    var percent = Number(task.percent);
    if (!isFinite(percent) || percent < 0) percent = 0;
    if (status === 'done') percent = 100;
    percent = clamp(percent, 0, 100);

    // 运行中但还没有进度 -> 显示不确定态动画
    var indeterminate = (status === 'running' || status === 'pending') && !(Number(task.percent) > 0);
    node.bar.classList.toggle('is-indeterminate', indeterminate);
    node.barFill.style.width = indeterminate ? '32%' : percent.toFixed(1) + '%';
    node.pct.textContent = indeterminate ? '—' : percent.toFixed(1) + '%';

    var sp = fmtSpeed(task.speed);
    node.speed.textContent = (status === 'running' && sp) ? sp : '';
    var eta = (status === 'running') ? fmtEta(task.eta) : '';
    node.eta.textContent = eta;
    node.msg.textContent = (task.message && status !== 'error') ? String(task.message) : '';

    if (status === 'error') {
      node.errBox.hidden = false;
      node.errBox.textContent = '✕ ' + (task.error || '任务执行失败，未返回具体原因');
    } else {
      node.errBox.hidden = true;
      node.errBox.textContent = '';
    }

    var outPath = (status === 'done') ? taskResultPath(task) : '';
    if (outPath) {
      node.resultBox.hidden = false;
      node.pathEl.textContent = outPath;
      node.pathEl.title = outPath;
    } else {
      node.resultBox.hidden = true;
      node.pathEl.textContent = '';
    }

    var active = (status === 'pending' || status === 'running');
    node.btnCancel.hidden = !active;
    node.btnCancel.disabled = false;
    node.btnCancel.textContent = '取消';
    // 进行中的任务用「取消」，已结束的任务才提供「从列表移除」
    node.btnRemove.hidden = active;
  }

  function upsertTask(task, synthetic) {
    if (!task || !task.id) return;
    // 记录「本地乐观创建」的任务：服务端列表一时还没带上它时，避免行被刷掉
    if (synthetic) state.optimistic.set(task.id, task);
    else state.optimistic.delete(task.id);
    state.tasks.set(task.id, task);
    var node = state.taskNodes.get(task.id);
    if (!node) {
      node = buildTaskRow(task);
      state.taskNodes.set(task.id, node);
      state.taskOrder.unshift(task.id);
      $('#task-list').insertBefore(node.row, $('#task-list').firstChild);
    }
    updateTaskRow(node, task);
    if (state.pendingHighlight.has(task.id)) {
      state.pendingHighlight.delete(task.id);
      node.row.classList.add('flash');
      setTimeout(function () { node.row.classList.remove('flash'); }, 1700);
    }
    updateTaskSummary();
  }

  function renderTasksFull(tasks) {
    var list = $('#task-list');
    list.textContent = '';
    state.tasks.clear();
    state.taskNodes.clear();
    state.taskOrder = [];
    var sorted = tasks.slice().sort(function (a, b) {
      return (Number(b && b.created) || 0) - (Number(a && a.created) || 0);
    });
    sorted.forEach(function (t) {
      if (!t || !t.id) return;
      state.optimistic.delete(t.id);
      state.tasks.set(t.id, t);
      var node = buildTaskRow(t);
      state.taskNodes.set(t.id, node);
      state.taskOrder.push(t.id);
      list.appendChild(node.row);
      updateTaskRow(node, t);
    });
    // 补回刚提交、服务端列表里还没出现的任务（SSE 稍后会推送真正的进度）
    var now = Date.now() / 1000;
    state.optimistic.forEach(function (t, id) {
      if (state.tasks.has(id)) return;
      if (now - (Number(t.created) || now) > OPTIMISTIC_TTL) { state.optimistic.delete(id); return; }
      state.tasks.set(id, t);
      var node = buildTaskRow(t);
      state.taskNodes.set(id, node);
      state.taskOrder.push(id);
      list.appendChild(node.row);
      updateTaskRow(node, t);
    });
    updateTaskSummary();
  }

  function updateTaskSummary() {
    var total = state.tasks.size;
    var running = 0, pending = 0, done = 0, failed = 0;
    state.tasks.forEach(function (t) {
      var s = t.status;
      if (s === 'running') running++;
      else if (s === 'pending') pending++;
      else if (s === 'done') done++;
      else if (s === 'error' || s === 'canceled') failed++;
    });
    var countEl = $('#task-count');
    countEl.textContent = String(total);
    countEl.className = 'pill' + (running || pending ? ' pill-accent' : (failed ? ' pill-warn' : ''));
    var parts = [];
    if (running) parts.push(running + ' 个进行中');
    if (pending) parts.push(pending + ' 个排队中');
    if (done) parts.push(done + ' 个已完成');
    if (failed) parts.push(failed + ' 个失败 / 已取消');
    $('#tasks-summary').textContent = parts.length ? parts.join(' · ') : (total ? '暂无进行中的任务' : '暂无任务');
    $('#tasks-empty').hidden = total > 0;
  }

  async function refreshTasks() {
    try {
      var res = await api('/api/tasks');
      var tasks = Array.isArray(res && res.tasks) ? res.tasks : [];
      renderTasksFull(tasks);
      setSseStatus(true);
      return tasks;
    } catch (e) {
      // 拉取失败不打扰用户，SSE 会自动重连后再次补齐
      console.warn('[tasks] 拉取任务列表失败：' + e.message);
      return null;
    }
  }

  async function cancelTask(id) {
    var node = state.taskNodes.get(id);
    if (node) { node.btnCancel.disabled = true; node.btnCancel.textContent = '取消中…'; }
    try {
      await api('/api/cancel', { task_id: id });
      toast('已请求取消该任务', 'info');
    } catch (e) {
      toast('取消失败：' + e.message, 'error');
      if (node) { node.btnCancel.disabled = false; node.btnCancel.textContent = '取消'; }
    }
  }

  async function clearTask(id) {
    try {
      await api('/api/clear', { task_id: id });
      removeTaskRow(id);
    } catch (e) {
      toast('移除任务失败：' + e.message, 'error');
    }
  }

  function removeTaskRow(id) {
    var node = state.taskNodes.get(id);
    if (node && node.row.parentNode) node.row.parentNode.removeChild(node.row);
    state.taskNodes.delete(id);
    state.tasks.delete(id);
    state.optimistic.delete(id);
    state.taskOrder = state.taskOrder.filter(function (x) { return x !== id; });
    updateTaskSummary();
  }

  async function clearFinished() {
    var finished = [];
    state.tasks.forEach(function (t) {
      if (t.status === 'done' || t.status === 'error' || t.status === 'canceled') finished.push(t.id);
    });
    if (!finished.length) {
      toast('没有已完成的任务可以清除', 'info');
      return;
    }
    var btn = $('#btn-clear-finished');
    setBusy(btn, true, '清除中…');
    try {
      await api('/api/clear', { finished_only: true });
      finished.forEach(removeTaskRow);
      toast('已清除 ' + finished.length + ' 个任务', 'success');
    } catch (e) {
      toast('清除失败：' + e.message, 'error');
    } finally {
      setBusy(btn, false);
    }
  }

  function focusTaskPanel() {
    var panel = $('#task-panel');
    if (!panel) return;
    panel.classList.remove('collapsed');
    $('#btn-tasks-toggle').setAttribute('aria-expanded', 'true');
    panel.classList.add('pulse');
    setTimeout(function () { panel.classList.remove('pulse'); }, 1600);
    try { panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); } catch (e) { /* 忽略 */ }
  }

  function setSseStatus(ok, text) {
    var el = $('#sse-status');
    var txt = $('#sse-text');
    if (!el || !txt) return;
    if (ok) {
      el.classList.add('is-on');
      el.classList.remove('is-off');
      txt.textContent = '实时连接正常';
    } else {
      el.classList.remove('is-on');
      el.classList.add('is-off');
      txt.textContent = text || '连接已断开';
    }
  }

  function connectEvents() {
    if (state.es) {
      try { state.es.close(); } catch (e) { /* 忽略 */ }
      state.es = null;
    }
    clearTimeout(state.esTimer);
    state.esTimer = null;

    setSseStatus(false, state.esEverConnected ? '正在重连…' : '正在连接…');

    var es;
    try {
      es = new EventSource('/api/events');
    } catch (e) {
      scheduleReconnect();
      return;
    }
    state.es = es;

    es.onopen = function () {
      state.esRetry = 0;
      state.esEverConnected = true;
      setSseStatus(true);
      refreshTasks();
    };

    es.onmessage = function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (!msg || typeof msg !== 'object') return;
      if (msg.type === 'ping') return;
      if (msg.type === 'hello') {
        state.esRetry = 0;
        state.esEverConnected = true;
        setSseStatus(true);
        refreshTasks();
        return;
      }
      if (msg.type === 'task' && msg.task) { upsertTask(msg.task); return; }
      if (msg.type === 'tasks' && Array.isArray(msg.tasks)) { renderTasksFull(msg.tasks); return; }
    };

    es.onerror = function () {
      setSseStatus(false, '连接已断开，正在重连…');
      scheduleReconnect();
    };
  }

  function scheduleReconnect() {
    if (state.es) {
      try { state.es.close(); } catch (e) { /* 忽略 */ }
      state.es = null;
    }
    if (state.esTimer) return;
    var delay = Math.min(SSE_MAX_BACKOFF, 1000 * Math.pow(2, state.esRetry));
    state.esRetry += 1;
    setSseStatus(false, '连接已断开，' + Math.round(delay / 1000) + 's 后重连…');
    state.esTimer = setTimeout(function () {
      state.esTimer = null;
      connectEvents();
    }, delay);
  }

  /* ======================================================================
   * 9. settings —— 设置弹窗
   * ==================================================================== */

  function applySettings(s) {
    state.settings = s || {};

    // NVENC 可用性
    var nvenc = $('#use-nvenc');
    var hasNvenc = !!state.settings.has_nvenc;
    nvenc.disabled = !hasNvenc;
    if (!hasNvenc) nvenc.checked = false;
    $('#nvenc-hint').textContent = hasNvenc ? '' : '当前 FFmpeg 未检测到 NVENC，硬件加速不可用';
    $('#nvenc-hint').className = hasNvenc ? 'hint' : 'hint hint-warn';
    $('#nvenc-check').title = hasNvenc ? '' : '当前 FFmpeg 未检测到 NVENC 编码器';

    // 下载目录提示
    var dir = state.settings.download_dir || '';
    var hint = $('#download-dir-hint');
    hint.textContent = dir ? '下载目录：' + dir : '';
    hint.title = dir;

    renderSrcWarn();

    // 弹窗字段
    $('#set-download-dir').value = state.settings.download_dir || '';
    $('#set-output-dir').value = state.settings.output_dir || '';
    $('#set-concurrency').value = state.settings.concurrency || 4;
    $('#set-prefer-codec').value = state.settings.prefer_codec || 'h264';
    $('#set-cookie-bilibili').checked = !!state.settings.cookie_bilibili;
    $('#set-cookie-douyin').checked = !!state.settings.cookie_douyin;

    // 只读信息
    var kv = $('#settings-kv');
    kv.textContent = '';
    var rows = [
      ['FFmpeg', state.settings.has_ffmpeg ? '已就绪' : '未检测到'],
      ['FFmpeg 路径', state.settings.ffmpeg_path || '—'],
      ['NVENC 硬件加速', state.settings.has_nvenc ? '可用' : '不可用'],
      ['B 站 Cookie', state.settings.cookie_bilibili ? '已启用' : '未启用'],
      ['抖音 Cookie', state.settings.cookie_douyin ? '已启用' : '未启用']
    ];
    rows.forEach(function (r) {
      kv.appendChild(h('div', { class: 'kv-row' },
        h('span', { class: 'kv-key', text: r[0] }),
        h('span', { class: 'kv-val', title: String(r[1]), text: String(r[1]) })
      ));
    });
  }

  async function loadSettings(opts) {
    opts = opts || {};
    try {
      var s = await api('/api/settings');
      applySettings(s);
      return s;
    } catch (e) {
      if (!opts.silent) toast('读取设置失败：' + e.message, 'error');
      else console.warn('[settings] 读取失败：' + e.message);
      return null;
    }
  }

  async function saveSettings() {
    var btn = $('#btn-settings-save');
    var payload = {
      download_dir: String($('#set-download-dir').value || '').trim(),
      output_dir: String($('#set-output-dir').value || '').trim(),
      concurrency: clamp(num($('#set-concurrency').value, 4), 1, 16),
      prefer_codec: $('#set-prefer-codec').value || 'h264',
      cookie_bilibili: $('#set-cookie-bilibili').checked,
      cookie_douyin: $('#set-cookie-douyin').checked
    };
    setBusy(btn, true, '保存中…');
    try {
      await api('/api/settings', payload);
      toast('设置已保存', 'success');
      await loadSettings({ silent: true });
      closeSettings();
    } catch (e) {
      toast('保存设置失败：' + e.message, 'error');
    } finally {
      setBusy(btn, false);
    }
  }

  function openSettings() {
    $('#settings-modal').hidden = false;
    loadSettings({ silent: true });
  }

  function closeSettings() {
    $('#settings-modal').hidden = true;
  }

  /* ======================================================================
   * 10. init —— 事件绑定与启动
   * ==================================================================== */

  function bindTabs() {
    $$('#tabs .tab').forEach(function (btn) {
      btn.addEventListener('click', function () { activateTab(btn.dataset.tab); });
    });
  }

  function bindParse() {
    $('#btn-parse').addEventListener('click', doParse);
    $('#btn-parse-clear').addEventListener('click', function () {
      $('#parse-input').value = '';
      setHint('#parse-status', '');
      $('#parse-input').focus();
    });
    $('#parse-input').addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        doParse();
      }
    });
    $('#btn-select-all').addEventListener('click', function () {
      state.cardNodes.forEach(function (node, id) {
        node.checkbox.checked = true;
        node.row.classList.add('is-selected');
        state.selected.add(id);
      });
      updateSelectionUI();
    });
    $('#btn-select-invert').addEventListener('click', function () {
      state.cardNodes.forEach(function (node, id) {
        var next = !node.checkbox.checked;
        node.checkbox.checked = next;
        node.row.classList.toggle('is-selected', next);
        if (next) state.selected.add(id); else state.selected.delete(id);
      });
      updateSelectionUI();
    });
    $('#btn-download').addEventListener('click', doDownload);
  }

  function bindImport() {
    var dz = $('#dropzone');
    dz.addEventListener('click', pickLocalFiles);
    dz.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pickLocalFiles(); }
    });
    ['dragenter', 'dragover'].forEach(function (type) {
      dz.addEventListener(type, function (e) {
        e.preventDefault();
        e.stopPropagation();
        if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
        dz.classList.add('is-over');
      });
    });
    ['dragleave', 'dragend'].forEach(function (type) {
      dz.addEventListener(type, function (e) {
        e.preventDefault();
        if (type === 'dragleave' && dz.contains(e.relatedTarget)) return;
        dz.classList.remove('is-over');
      });
    });
    dz.addEventListener('drop', function (e) {
      e.preventDefault();
      e.stopPropagation();
      dz.classList.remove('is-over');
      var files = [];
      try {
        files = Array.prototype.slice.call((e.dataTransfer && e.dataTransfer.files) || []);
      } catch (err) { files = []; }
      if (!files.length) {
        toast('没有读取到拖入的文件，请重试或点击投放区使用系统文件选择框', 'warn');
        return;
      }
      var noRealPath = 0;
      var paths = [];
      files.forEach(function (f) {
        var p = f.path || f.webkitRelativePath || '';
        if (!p) { noRealPath++; p = f.name; }
        if (p) paths.push(p);
      });
      if (noRealPath) {
        toast('浏览器未提供 ' + noRealPath + ' 个文件的真实路径，已尝试用文件名导入；若失败请点击投放区改用系统文件选择框', 'warn', { duration: 6000 });
      }
      importByPaths(paths);
    });
    // 阻止浏览器默认的“打开文件”行为
    window.addEventListener('dragover', function (e) { e.preventDefault(); });
    window.addEventListener('drop', function (e) {
      if (e.target !== dz && !dz.contains(e.target)) e.preventDefault();
    });
  }

  function bindEdit() {
    $('#btn-pick-input').addEventListener('click', pickInputFile);
    $('#btn-load-input').addEventListener('click', function () { loadEditPath($('#edit-path').value); });
    $('#edit-path').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); loadEditPath($('#edit-path').value); }
    });

    var v = $('#preview');
    v.addEventListener('loadedmetadata', function () {
      if (!(state.cur.duration > 0) && isFinite(v.duration) && v.duration > 0) {
        resetTrimRange(v.duration);
      }
      renderSourceInfo();
    });
    v.addEventListener('error', function () {
      if (!state.cur.path) return;
      toast('预览加载失败：无法读取该文件，请确认路径存在且为可播放的视频', 'error');
    });

    // 裁剪：启用开关
    $('#trim-enabled').addEventListener('change', function () {
      var on = trimEnabled();
      if (on && !(state.cur.duration > 0)) {
        toast('尚未读取到视频时长，裁剪滑块不可用；可直接在输入框填写起止秒数', 'warn');
      }
      if (on) {
        var el = trimRangeEls();
        if (num(el.er.value, 0) <= 0 && state.cur.duration > 0) {
          applyTrimRange(0, state.cur.duration, { skipSnap: true });
        }
      }
      syncTrimUI();
    });

    // 裁剪：数字输入框（输入时只同步滑块，失焦 / 回车时归一化并吸附关键帧）
    var el = trimRangeEls();
    el.si.addEventListener('input', syncFromNumberInputs);
    el.ei.addEventListener('input', syncFromNumberInputs);
    el.si.addEventListener('change', function () {
      applyTrimRange(num(el.si.value, 0), num(el.ei.value, state.cur.duration), { snapStart: true });
    });
    el.ei.addEventListener('change', function () {
      applyTrimRange(num(el.si.value, 0), num(el.ei.value, state.cur.duration), { snapEnd: true });
    });

    // 裁剪：双滑块
    el.sr.addEventListener('input', function () { syncFromRanges('start'); });
    el.er.addEventListener('input', function () { syncFromRanges('end'); });
    el.sr.addEventListener('change', function () {
      applyTrimRange(num(el.sr.value, 0), num(el.er.value, state.cur.duration), { snapStart: true });
    });
    el.er.addEventListener('change', function () {
      applyTrimRange(num(el.sr.value, 0), num(el.er.value, state.cur.duration), { snapEnd: true });
    });

    $('#btn-set-start').addEventListener('click', setStartFromPlayer);
    $('#btn-set-end').addEventListener('click', setEndFromPlayer);

    // 裁剪模式
    $$('#trim-modes input[name="trim-mode"]').forEach(function (r) {
      r.addEventListener('change', syncTrimModeUI);
    });

    // 压缩模式 / CRF
    $$('#compress-modes input[name="compress-mode"]').forEach(function (r) {
      r.addEventListener('change', syncCompressUI);
    });
    $$('input[name="crf"]').forEach(function (r) {
      r.addEventListener('change', syncCompressUI);
    });

    $('#btn-estimate').addEventListener('click', doEstimate);
    $('#btn-edit').addEventListener('click', doEdit);
  }

  function bindTasks() {
    $('#btn-tasks-toggle').addEventListener('click', function () {
      var panel = $('#task-panel');
      var collapsed = panel.classList.toggle('collapsed');
      $('#btn-tasks-toggle').setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    });
    $('#btn-clear-finished').addEventListener('click', clearFinished);
  }

  function bindSettings() {
    $('#btn-settings').addEventListener('click', openSettings);
    $('#btn-settings-close').addEventListener('click', closeSettings);
    $('#btn-settings-reload').addEventListener('click', function () {
      loadSettings().then(function (s) { if (s) toast('设置已重新读取', 'info'); });
    });
    $('#btn-settings-save').addEventListener('click', saveSettings);
    $$('#settings-modal [data-close]').forEach(function (el) {
      el.addEventListener('click', closeSettings);
    });
  }

  function bindGlobal() {
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !$('#settings-modal').hidden) closeSettings();
    });
    window.addEventListener('beforeunload', function () {
      if (state.es) { try { state.es.close(); } catch (err) { /* 忽略 */ } }
    });
  }

  function init() {
    bindTabs();
    bindParse();
    bindImport();
    bindEdit();
    bindWatermark();
    bindTasks();
    bindSettings();
    bindGlobal();

    // 初始化静态文案
    syncTrimModeUI();
    syncCompressUI();
    resetTrimRange(0);
    renderSourceInfo();
    updateSelectionUI();
    updateTaskSummary();
    wmRenderList();
    wmSyncUI();

    // 从 URL hash 恢复标签页
    var hash = String(location.hash || '').replace('#', '');
    activateTab(['parse', 'import', 'edit'].indexOf(hash) >= 0 ? hash : 'parse');

    loadSettings({ silent: true });
    refreshTasks();
    connectEvents();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // 便于在浏览器控制台里调试
  window.__APP__ = {
    state: state,
    api: api,
    toast: toast,
    refreshTasks: refreshTasks,
    parse: doParse,
    loadEditPath: loadEditPath,
    // 去水印调试入口
    watermark: {
      boxes: function () { return state.wm.boxes; },
      payload: wmPayload,
      redraw: function () { wmRenderList(); wmSyncUI(); },
      frame: wmRequestFrame
    }
  };

})();
