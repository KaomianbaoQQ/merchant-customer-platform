/* 中英文切换内核：给页面提供 window.KB_I18N。
   用法：页面先引本文件，再引各自的语言包（i18n/客户.js 等），语言包调用 KB_I18N.extend({...})。
   语言包格式：{'中文原文': 'English', ...}，键里有 ${a} 这种变量时按占位符匹配。 */
(function (global) {
  'use strict';

  var STORE_KEY = 'kb_lang';
  var ZH = '\u4e2d\u6587';
  var dict = Object.create(null);
  var patterns = [];
  var originals = new Map();       // 文本节点 -> 原始中文，切回中文时还原
  var attrOriginals = [];          // [元素, 属性名, 原文]
  var lang = 'zh';
  var titleZh = '';
  var booted = false;
  var observer = null;
  var pending = [];

  var TEXT_ATTRS = ['placeholder', 'title', 'aria-label', 'alt'];
  var SKIP_TAGS = /^(SCRIPT|STYLE|NOSCRIPT|TEXTAREA|INPUT|CODE|PRE)$/;
  var CJK = /[\u4e00-\u9fa5]/;

  function norm(s) {
    return String(s).replace(/\s+/g, ' ').trim();
  }

  function has(obj, key) {
    return Object.prototype.hasOwnProperty.call(obj, key);
  }

  function escapeRe(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  function buildPatterns() {
    patterns = [];
    Object.keys(dict).forEach(function (key) {
      if (key.indexOf('${') === -1) return;
      var idx = 0;
      var body = escapeRe(key).replace(/\\\$\\\{[^}]*\\\}/g, function () {
        idx += 1;
        return '([\\s\\S]*?)';
      });
      var en = dict[key];
      var n = 0;
      en = en.replace(/\$\{[^}]*\}/g, function () {
        n += 1;
        return '$' + n;
      });
      try {
        patterns.push({ re: new RegExp('^' + body + '$'), en: en, len: key.length });
      } catch (e) { /* 忽略写错的正则 */ }
    });
    patterns.sort(function (a, b) { return b.len - a.len; });
  }

  function lookup(text) {
    var key = norm(text);
    if (!key) return null;
    if (has(dict, key)) return dict[key];
    for (var i = 0; i < patterns.length; i++) {
      if (patterns[i].re.test(key)) return key.replace(patterns[i].re, patterns[i].en);
    }
    return null;
  }

  function injectStyle() {
    if (document.getElementById('kb-i18n-style')) return;
    var css = ''
      + '.kb-lang-btn{font:inherit;font-size:12px;line-height:1;padding:6px 10px;border-radius:999px;'
      + 'border:1px solid rgba(0,0,0,.18);background:rgba(255,255,255,.7);color:inherit;cursor:pointer;'
      + 'transition:background .2s,opacity .2s;white-space:nowrap;opacity:.85}'
      + '.kb-lang-btn:hover{opacity:1;background:rgba(0,0,0,.06)}'
      + '.kb-lang-fixed{position:fixed;top:14px;right:14px;z-index:9999;box-shadow:0 2px 10px rgba(0,0,0,.12)}';
    var style = document.createElement('style');
    style.id = 'kb-i18n-style';
    style.textContent = css;
    document.head.appendChild(style);
  }

  function mountToggle() {
    if (document.querySelector('.kb-lang-btn')) return;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'kb-lang-btn';
    btn.setAttribute('data-no-i18n', '1');
    btn.textContent = lang === 'en' ? ZH : 'EN';
    btn.title = lang === 'en' ? 'Switch to Chinese' : 'Switch to English';
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      toggle();
    });
    var host = null;
    var selectors = ['[data-lang-host]', '.nav-right', 'header.top', 'header'];
    for (var i = 0; i < selectors.length; i++) {
      var el = document.querySelector(selectors[i]);
      if (el && el.offsetHeight > 0) { host = el; break; }
    }
    if (!host) {
      host = document.body;
      btn.classList.add('kb-lang-fixed');
    }
    host.appendChild(btn);
  }

  function restore() {
    originals.forEach(function (raw, node) {
      if (node && node.nodeValue !== raw) node.nodeValue = raw;
    });
    originals.clear();
    attrOriginals.forEach(function (item) {
      if (item[0] && item[0].getAttribute(item[1]) !== item[2]) item[0].setAttribute(item[1], item[2]);
    });
    attrOriginals.length = 0;
  }

  function translateNode(node) {
    if (originals.has(node)) return;
    var raw = node.nodeValue;
    if (!raw || !CJK.test(raw)) return;
    var m = /^(\s*)([\s\S]*?)(\s*)$/.exec(raw);
    var hit = lookup(m[2]);
    if (hit == null) return;
    originals.set(node, raw);
    node.nodeValue = m[1] + hit + m[3];
  }

  function translateAttrs(root) {
    if (!root.querySelectorAll) return;
    TEXT_ATTRS.forEach(function (attr) {
      var nodes = [];
      if (root.nodeType === 1 && root.hasAttribute(attr)) nodes.push(root);
      root.querySelectorAll('[' + attr + ']').forEach(function (el) {
        if (/^(SCRIPT|STYLE|NOSCRIPT)$/.test(el.tagName)) return;
        nodes.push(el);
      });
      nodes.forEach(function (el) {
        var raw = el.getAttribute(attr);
        if (!raw || !CJK.test(raw)) return;
        var hit = lookup(raw);
        if (hit == null) return;
        attrOriginals.push([el, attr, raw]);
        el.setAttribute(attr, hit);
      });
    });
  }

  function walk(root) {
    if (!root) return;
    if (root.nodeType === 3) { translateNode(root); return; }
    if (root.nodeType !== 1) return;
    if (SKIP_TAGS.test(root.tagName) || root.hasAttribute('data-no-i18n')) return;
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        var parent = node.parentNode;
        if (!parent) return NodeFilter.FILTER_REJECT;
        if (SKIP_TAGS.test(parent.tagName)) return NodeFilter.FILTER_REJECT;
        if (parent.closest && parent.closest('[data-no-i18n]')) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    while (walker.nextNode()) translateNode(walker.currentNode);
    translateAttrs(root);
  }

  function applyAll() {
    walk(document.body);
    var title = lookup(titleZh || document.title);
    if (title != null) document.title = title;
  }

  function flush() {
    if (!pending.length) return;
    var list = pending.slice();
    pending.length = 0;
    observer.disconnect();
    list.forEach(function (node) {
      if (!node || !node.isConnected) return;
      if (node.nodeType === 3) {
        if (!node.parentNode || node.parentNode.closest('[data-no-i18n]')) return;
        translateNode(node);
      } else {
        walk(node);
      }
    });
    observe();
  }

  function queue(node) {
    if (!node) return;
    pending.push(node);
    if (pending.length === 1) Promise.resolve().then(flush);
  }

  function observe() {
    if (!observer || !document.body) return;
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
  }

  function setLang(next) {
    lang = next === 'en' ? 'en' : 'zh';
    document.documentElement.lang = lang === 'en' ? 'en' : 'zh-CN';
    try { localStorage.setItem(STORE_KEY, lang); } catch (e) {}
    var btn = document.querySelector('.kb-lang-btn');
    if (btn) {
      btn.textContent = lang === 'en' ? ZH : 'EN';
      btn.title = lang === 'en' ? 'Switch to Chinese' : 'Switch to English';
    }
    if (lang === 'en') {
      applyAll();
    } else {
      restore();
      if (titleZh) document.title = titleZh;
    }
  }

  function toggle() {
    setLang(lang === 'en' ? 'zh' : 'en');
  }

  function extend(extra) {
    Object.keys(extra).forEach(function (k) {
      dict[norm(k)] = extra[k];
    });
    buildPatterns();
    if (booted && lang === 'en') applyAll();
  }

  /* alert / confirm / prompt 的文案也走词条，不然英文模式下会弹中文 */
  function wrapDialogs() {
    ['alert', 'confirm', 'prompt'].forEach(function (name) {
      var raw = global[name];
      if (typeof raw !== 'function' || raw.kbWrapped) return;
      var wrapped = function () {
        var args = Array.prototype.slice.call(arguments);
        if (lang === 'en' && typeof args[0] === 'string') {
          var hit = lookup(args[0]);
          if (hit != null) args[0] = hit;
        }
        return raw.apply(global, args);
      };
      wrapped.kbWrapped = true;
      global[name] = wrapped;
    });
  }

  function boot() {
    if (booted) return;
    booted = true;
    titleZh = document.title;
    wrapDialogs();
    try {
      var fromUrl = new URLSearchParams(location.search).get('lang');
      if (fromUrl) localStorage.setItem(STORE_KEY, fromUrl === 'en' ? 'en' : 'zh');
      lang = localStorage.getItem(STORE_KEY) === 'en' ? 'en' : 'zh';
    } catch (e) { lang = 'zh'; }
    injectStyle();
    mountToggle();
    observer = new MutationObserver(function (records) {
      if (lang !== 'en') return;
      records.forEach(function (r) {
        if (r.type === 'characterData') queue(r.target);
        else r.addedNodes.forEach(queue);
      });
    });
    observe();
    if (lang === 'en') {
      document.documentElement.lang = 'en';
      applyAll();
    }
    window.addEventListener('load', function () {
      if (lang === 'en') applyAll();
    });
  }

  global.KB_I18N = {
    extend: extend,
    setLang: setLang,
    toggle: toggle,
    t: lookup,
    apply: applyAll,
    get lang() { return lang; },
    /* 调试用：列出页面里还没翻译的中文，方便补词条 */
    missing: function () {
      var set = Object.create(null);
      var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) {
        var node = walker.currentNode;
        var text = norm(node.nodeValue);
        if (!text || !CJK.test(text) || text.length > 80) continue;
        if (node.parentNode && node.parentNode.closest('[data-no-i18n]')) continue;
        if (lookup(text) == null) set[text] = (set[text] || 0) + 1;
      }
      return Object.keys(set).sort(function (a, b) { return set[b] - set[a]; });
    }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})(window);
