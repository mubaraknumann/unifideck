"""The "already owned elsewhere" ribbon, as JS evaluated inside a Steam Store page.

The Gaming Mode store is a separate BrowserView composited *above* the Big
Picture window, so nothing Unifideck renders in Steam's own React tree can
reach it. ProtonDB Badges and DeckySales hit the same wall and inject DOM
into the store document over CDP; this does the same.

Two placements, both verified on-device (2026-10-02):

* an **overlay strip inside the capsule-art container** (the parent of
  ``#gamepad_carousel img[src*="/header"]``). It is visible on first paint
  and *layout-neutral*. An in-flow banner above the media carousel left the
  carousel's own gamepad focus ring stranded at its old coordinates (they
  are computed at focus time, and neither a refocus nor a resize event
  recomputes them), so nothing above the carousel may change height;
* an **in-flow note just before ``#FeatureTarget_purchase-options``**. It
  sits below the initially focused carousel, so it shifts nothing the ring
  depends on, and it is at the point of purchase. The D-pad stops on it
  like on any native block, because it is built like one: its own
  navigation tree, focus-ring root and ``Focusable``, from the page's own
  React (verified on-device 2026-10-02 with D-pad presses: USER TAGS, the
  note with its outline, the purchase options, DLC, and back). If any of
  those cannot be found, the same note is drawn as plain DOM.

Every node is built with ``createElement``/``textContent``; store logos
arrive as SVG shape data (allowlisted by ``rpc/mixins/_store_ribbon_icons``)
and are rebuilt with ``createElementNS``/``setAttribute``. The payload is
data only, embedded by :func:`build_ribbon_script` as a JSON literal. The
script is idempotent (a window-scoped handle replaces or keeps a previous
instance) and self-verifying (it renders nothing unless ``location.pathname``
is still ``/app/<appid>``, so an evaluation that lost a race with the next
navigation does nothing). Hashed store class names change with every store
deploy; only ``id`` anchors are used.

The script answers ``'installed'``, ``'unchanged'``, ``'path-mismatch'``
(this document is not on that app page, typically the previous page still
in the target while the next one loads) or ``'error: ...'``.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

_RIBBON_FN_JS = r"""(function (DATA) {
  'use strict';
  var HANDLE = '__unifideckOwnershipRibbon';
  var STYLE_ID = 'unifideck-owned-style';
  var OVERLAY_ID = 'unifideck-owned-overlay';
  var NOTE_ID = 'unifideck-owned-note';
  var OVERLAY_ANCHOR = '#gamepad_carousel img[src*="/header"]';
  var NOTE_ANCHOR = '#FeatureTarget_purchase-options';
  var GUTTER_REF = '#FeatureTarget_summary-bar-top';
  var GIVE_UP_MS = 15000;
  var SVG_NS = 'http://www.w3.org/2000/svg';
  // Defence in depth behind the backend allowlist (rpc/mixins/_store_ribbon_icons.py,
  // ICON_TAGS; a test pins this regex to that set).
  var ICON_TAG_RE = /^(?:path|circle|ellipse|rect|polygon|polyline|line|g)$/;
  var ICON_ATTR_RE = /^[a-z][a-z-]*$/;
  var NAV_ID = 'unifideck-owned-nav';
  var PAGE_REACT = '__unifideckPageReact';
  var CONTEXT_TYPE = Symbol.for('react.context');
  var CSS = [
    '.ud-own-host{margin:0 34px 16px}',
    '.ud-own-note{box-sizing:border-box;padding:12px 24px;background:rgba(14,20,27,.8);',
    'font-family:"Motiva Sans",Arial,sans-serif;color:#c6d4df;',
    'font-size:15px;line-height:1.3;pointer-events:none}',
    '.ud-own-body{display:flex;flex-direction:column;gap:8px}',
    '.ud-own-row{display:flex;align-items:center;flex-wrap:wrap;gap:8px 12px}',
    '.ud-own-tag{background:#a1cd44;color:#111113;font-size:11px;padding:1px 6px;letter-spacing:.04em;',
    'text-transform:uppercase;white-space:nowrap}',
    '.ud-own-msg{color:#a1cd44}',
    '.ud-own-chips{display:flex;gap:6px;flex-wrap:wrap}',
    '.ud-own-chip{display:inline-flex;align-items:center;gap:6px;background:rgba(255,255,255,.1);',
    'color:#dcdedf;font-size:13px;padding:2px 8px;border-radius:2px}',
    '.ud-own-dot{width:7px;height:7px;border-radius:50%;background:#67707b;flex:none}',
    '.ud-own-chip.ud-inst .ud-own-dot{background:#a1cd44}',
    '.ud-own-logo{width:16px;height:16px;flex:none}',
    '.ud-own-chip.ud-inst .ud-own-logo{color:#a1cd44}',
    '.ud-own-detail{color:#8f98a0}',
    // A bullet before every note, so "Xbox, Play Anywhere, Cloud" read as
    // separate facts. The CSS escape keeps this script ASCII.
    '.ud-own-detail::before{content:"\\2022";margin-inline-end:6px;color:#dcdedf;font-weight:700}',
    '.ud-own-via{margin-inline-start:auto;font-size:12px;color:#8f98a0}',
    '.ud-own-ovl{position:absolute;left:0;right:0;top:0;z-index:2;box-sizing:border-box;padding:6px 10px;',
    'display:flex;align-items:center;gap:8px;white-space:nowrap;pointer-events:none;',
    'background:linear-gradient(90deg,rgba(14,20,27,.92),rgba(14,20,27,.55));',
    'font-family:"Motiva Sans",Arial,sans-serif;font-size:12px;color:#a1cd44}',
    '.ud-own-ovl[dir=rtl]{background:linear-gradient(270deg,rgba(14,20,27,.92),rgba(14,20,27,.55))}',
    '.ud-own-ovl .ud-own-tag{font-size:10px;padding:1px 5px}',
    '.ud-own-ovl-text{overflow:hidden;text-overflow:ellipsis;min-width:0}'
  ].join('');

  try {
    var pathRe = new RegExp('^/app/' + Number(DATA.appid) + '(?:/|$)');
    var pathOk = function () { return pathRe.test(window.location.pathname); };
    // Before touching any previous instance: an evaluation that lost the
    // race with the next navigation must not tear down a valid ribbon.
    if (!pathOk()) return 'path-mismatch';
    var key = JSON.stringify(DATA);
    var prev = window[HANDLE];
    if (prev && prev.key === key && prev.alive) return 'unchanged';
    if (prev && typeof prev.teardown === 'function') {
      try { prev.teardown(); } catch (e) { /* a broken predecessor must not block this one */ }
    }

    var el = function (tag, cls, text) {
      var node = document.createElement(tag);
      if (cls) node.className = cls;
      if (text !== undefined && text !== null) node.textContent = String(text);
      return node;
    };

    var buildOverlay = function () {
      var o = DATA.overlay;
      var root = el('div', 'ud-own-ovl');
      root.id = OVERLAY_ID;
      root.dir = DATA.dir;
      root.tabIndex = -1;
      root.appendChild(el('span', 'ud-own-tag', o.tag));
      root.appendChild(el('span', 'ud-own-ovl-text', o.text));
      return root;
    };

    // Store logos arrive as allowlisted SVG data and are rebuilt node by node
    // with createElementNS/setAttribute. Nothing is parsed from markup.
    var appendShapes = function (parent, nodes, depth) {
      if (depth > 3 || !nodes || !nodes.length) return;
      for (var i = 0; i < nodes.length; i++) {
        var n = nodes[i];
        if (!n || !ICON_TAG_RE.test(String(n.tag))) continue;
        var shape = document.createElementNS(SVG_NS, n.tag);
        var attrs = n.attrs || {};
        for (var name in attrs) {
          if (!Object.prototype.hasOwnProperty.call(attrs, name)) continue;
          if (!ICON_ATTR_RE.test(name) || name.indexOf('on') === 0 || name.indexOf('href') !== -1) continue;
          shape.setAttribute(name, String(attrs[name]));
        }
        appendShapes(shape, n.children, depth + 1);
        parent.appendChild(shape);
      }
    };

    var buildLogo = function (icon) {
      if (!icon || typeof icon.viewBox !== 'string' || !icon.nodes || !icon.nodes.length) return null;
      var svg = document.createElementNS(SVG_NS, 'svg');
      svg.setAttribute('viewBox', icon.viewBox);
      svg.setAttribute('class', 'ud-own-logo');
      svg.setAttribute('fill', 'currentColor');
      svg.setAttribute('aria-hidden', 'true');
      appendShapes(svg, icon.nodes, 0);
      return svg;
    };

    var buildChip = function (c) {
      var chip = el('span', 'ud-own-chip' + (c.installed ? ' ud-inst' : ''));
      var logo = buildLogo(c.icon);
      if (logo) chip.appendChild(logo);
      else if (c.label) chip.appendChild(el('span', 'ud-own-dot'));
      if (c.label) chip.appendChild(el('span', null, c.label));
      if (c.detail) chip.appendChild(el('span', 'ud-own-detail', c.detail));
      for (var n = 0; n < (c.notes || []).length; n++) {
        chip.appendChild(el('span', 'ud-own-detail', String(c.notes[n])));
      }
      if (c.installed && DATA.installed) chip.appendChild(el('span', 'ud-own-detail', DATA.installed));
      return chip;
    };

    // Match the page's own side gutters (the summary bar's offset inside the
    // purchase block's parent); the CSS default is the 1280-wide layout's.
    var applyGutter = function (host, anchor) {
      var ref = document.querySelector(GUTTER_REF);
      var parent = anchor.parentElement;
      if (!ref || !parent) return;
      var r = ref.getBoundingClientRect();
      var p = parent.getBoundingClientRect();
      if (r.width <= 0 || p.width <= 0) return;
      host.style.marginLeft = Math.max(0, Math.round(r.left - p.left)) + 'px';
      host.style.marginRight = Math.max(0, Math.round(p.right - r.right)) + 'px';
    };

    var fillBody = function (body) {
      var sections = DATA.sections || [];
      for (var i = 0; i < sections.length; i++) {
        var s = sections[i];
        var row = el('div', 'ud-own-row');
        row.appendChild(el('span', 'ud-own-tag', s.tag));
        row.appendChild(el('span', 'ud-own-msg', s.message));
        if (s.chips && s.chips.length) {
          var chips = el('span', 'ud-own-chips');
          for (var j = 0; j < s.chips.length; j++) chips.appendChild(buildChip(s.chips[j]));
          row.appendChild(chips);
        }
        if (i === 0 && DATA.via) row.appendChild(el('span', 'ud-own-via', DATA.via));
        body.appendChild(row);
      }
    };

    var plainNote = function () {
      var note = el('div', 'ud-own-note');
      note.tabIndex = -1;
      var body = el('div', 'ud-own-body');
      fillBody(body);
      note.appendChild(body);
      return note;
    };

    // Gaming Mode moves focus with Valve's navigation library, which skips
    // plain DOM. So the note is built the way the page builds each of its
    // feature blocks: its own navigation tree (the page's tree component,
    // embedded in the page's legacy tree), the page's focus-ring root, and
    // the page's Focusable. The D-pad then moves onto it and off it like any
    // native block, and the same outline is drawn. Each piece is found by
    // what its code does, because the hashed names change with every store
    // deploy. If one is missing, the plain note is drawn instead.
    var fiberOf = function (node) {
      if (!node) return null;
      var keys = Object.keys(node);
      for (var i = 0; i < keys.length; i++) {
        if (keys[i].indexOf('__reactFiber$') === 0) return node[keys[i]];
      }
      return null;
    };

    // The page's React and ReactDOM, from its webpack registry. Scanned
    // once per document (about 30 ms on a Deck); a miss is remembered too.
    var scanPageReact = function () {
      var chunks = window.webpackChunkstore;
      if (!chunks || typeof chunks.push !== 'function') return null;
      var req = null;
      chunks.push([[Symbol('unifideck-ribbon')], {}, function (r) { req = r; }]);
      if (!req || !req.m) return null;
      var React = null;
      var ReactDOM = null;
      var ids = Object.keys(req.m);
      for (var i = 0; i < ids.length && !(React && ReactDOM); i++) {
        var src = String(req.m[ids[i]]);
        if (!React && src.indexOf('useState') !== -1 && /react\.(transitional\.)?element/.test(src)) {
          var r = req(ids[i]);
          if (r && typeof r.createElement === 'function' && typeof r.useState === 'function') React = r;
        }
        if (!ReactDOM && src.indexOf('createRoot') !== -1 && src.indexOf('hydrateRoot') !== -1) {
          var d = req(ids[i]);
          if (d && typeof d.createRoot === 'function') ReactDOM = d;
        }
      }
      return React && ReactDOM ? { React: React, ReactDOM: ReactDOM } : null;
    };

    var pageReact = function () {
      if (window[PAGE_REACT] === undefined) {
        try { window[PAGE_REACT] = scanPageReact(); } catch (e) { window[PAGE_REACT] = null; }
      }
      return window[PAGE_REACT];
    };

    // The nearest component fiber above *start* whose code contains every
    // needle.
    var fiberAbove = function (start, needles) {
      for (var f = start, depth = 0; f && depth < 40; depth++, f = f.return) {
        if (typeof f.type !== 'function') continue;
        var src = String(f.type);
        var all = true;
        for (var k = 0; k < needles.length && all; k++) all = src.indexOf(needles[k]) !== -1;
        if (all) return f;
      }
      return null;
    };

    // Every context the page's blocks see (navigation controller, gamepad
    // UI, router), nearest first. Only contexts ABOVE a block's tree
    // component: a copied value never updates, so the tree's own contexts
    // (whether it is active, its root node) come from a live tree instead.
    // A copied "active: false" was why focus reached the note but no
    // outline was drawn.
    var contextsAbove = function (fiber) {
      var out = [];
      var seen = [];
      for (var f = fiber; f; f = f.return) {
        var t = f.type;
        if (t && t.$$typeof === CONTEXT_TYPE && f.memoizedProps && 'value' in f.memoizedProps &&
            seen.indexOf(t) === -1) {
          seen.push(t);
          out.push([t, f.memoizedProps.value]);
        }
      }
      return out;
    };

    var navKit = function (anchor) {
      var lib = pageReact();
      var panel = fiberOf(anchor.querySelector('.Panel.Focusable') ||
        document.querySelector('[data-react-nav-root] .Panel.Focusable'));
      // Nearest first: the Focusable sits below the tree component, whose
      // code also mentions "flow-children".
      var focusable = fiberAbove(panel, ['"flow-children"']);
      var ringRoot = fiberAbove(panel, ['disableFocusRing', 'OnForceMeasureFocusRing']);
      var block = fiberAbove(fiberOf(anchor), ['NewGamepadNavigationTree']);
      var blockProps = block && block.memoizedProps;
      if (!lib || !focusable || !ringRoot || !blockProps || !blockProps.parentEmbeddedNavTree) return null;
      return {
        lib: lib,
        focusable: focusable.type,
        ringRoot: ringRoot.type,
        navTree: block.type,
        treeProps: {
          navID: NAV_ID,
          parentEmbeddedNavTree: blockProps.parentEmbeddedNavTree,
          historyMode: blockProps.historyMode,
          'flow-children': 'column'
        },
        contexts: contextsAbove(block.return)
      };
    };

    var noteRoot = null;
    var navBroken = false;

    var dropNote = function () {
      var root = noteRoot;
      noteRoot = null;
      if (root) { try { root.unmount(); } catch (e) { /* already gone with its host */ } }
      var host = document.getElementById(NOTE_ID);
      if (host) host.remove();
    };

    // A render error inside the page's components: plain notes from now on.
    var onRenderError = function () {
      navBroken = true;
      window.setTimeout(function () {
        if (!state.alive) return;
        dropNote();
        ensure();
      }, 0);
    };

    var mountFocusable = function (host, kit) {
      var h = kit.lib.React.createElement;
      var fill = function (body) { if (body && !body.firstChild) fillBody(body); };
      // The tree component gives its child div the block id and
      // data-react-nav-root, which the legacy tree uses to embed it.
      var tree = h(kit.navTree, kit.treeProps,
        h('div', null,
          h(kit.ringRoot, { disableFocusRing: false },
            h(kit.focusable, { focusable: true, className: 'ud-own-note' },
              h('div', { className: 'ud-own-body', ref: fill })))));
      for (var i = 0; i < kit.contexts.length; i++) {
        tree = h(kit.contexts[i][0], { value: kit.contexts[i][1] }, tree);
      }
      var root = kit.lib.ReactDOM.createRoot(host, {
        onUncaughtError: onRenderError,
        onCaughtError: onRenderError,
      });
      root.render(tree);
      return root;
    };

    var placeNote = function (anchor) {
      var host = el('div', 'ud-own-host');
      host.id = NOTE_ID;
      host.dir = DATA.dir;
      anchor.parentElement.insertBefore(host, anchor);
      applyGutter(host, anchor);
      var kit = navBroken ? null : navKit(anchor);
      if (kit) {
        try { noteRoot = mountFocusable(host, kit); return; } catch (e) { navBroken = true; }
      }
      host.appendChild(plainNote());
    };

    var state = { key: key, alive: true, teardown: null };
    var observer = null;
    var giveUp = 0;
    var queued = false;
    var found = false;

    var teardown = function () {
      if (!state.alive) return;
      state.alive = false;
      if (observer) observer.disconnect();
      window.clearTimeout(giveUp);
      dropNote();
      [OVERLAY_ID, STYLE_ID].forEach(function (id) {
        var node = document.getElementById(id);
        if (node) node.remove();
      });
      if (window[HANDLE] === state) window[HANDLE] = undefined;
    };
    state.teardown = teardown;

    var ensure = function () {
      if (!pathOk()) { teardown(); return; }
      var head = document.head || document.documentElement;
      if (head && !document.getElementById(STYLE_ID)) {
        var style = el('style', null, CSS);
        style.id = STYLE_ID;
        head.appendChild(style);
      }
      var art = document.querySelector(OVERLAY_ANCHOR);
      var capsule = art && art.parentElement;
      if (capsule && DATA.overlay && !document.getElementById(OVERLAY_ID)) {
        capsule.appendChild(buildOverlay());
      }
      var anchor = document.querySelector(NOTE_ANCHOR);
      if (anchor && anchor.parentElement && !document.getElementById(NOTE_ID)) {
        dropNote(); // a page re-render removed the host: release its root
        placeNote(anchor);
      }
      if (capsule || anchor) found = true;
    };

    window[HANDLE] = state;
    // The store page is React: anchors appear late and re-renders drop our
    // nodes. Re-check once per frame while the DOM is changing. The whole
    // document is observed: the draw can arrive while the new page is still
    // loading, before it has an <html> element.
    observer = new MutationObserver(function () {
      if (queued) return;
      queued = true;
      window.requestAnimationFrame(function () {
        queued = false;
        if (state.alive) ensure();
      });
    });
    observer.observe(document, { childList: true, subtree: true });
    giveUp = window.setTimeout(function () {
      if (found || !state.alive) return;
      console.info('[Unifideck] ownership ribbon not shown: neither ' + OVERLAY_ANCHOR +
        ' nor ' + NOTE_ANCHOR + ' exists on this store page layout');
      teardown();
    }, GIVE_UP_MS);
    ensure();
    return 'installed';
  } catch (e) {
    // A half-built instance must not stay registered: the next call would
    // see a live handle with the same key and keep it as 'unchanged'.
    if (state && state.alive) { try { state.teardown(); } catch (e2) { /* nothing left to undo */ } }
    return 'error: ' + (e && e.message ? e.message : String(e));
  }
})"""


def build_ribbon_script(payload: Mapping[str, Any]) -> str:
    """The ribbon function applied to *payload*, ready for ``Runtime.evaluate``.

    ``json.dumps`` output is a valid JS expression; ``ensure_ascii`` also
    escapes U+2028/U+2029, the two characters JSON allows raw but older JS
    parsers reject inside string literals. No other templating touches the
    function body.
    """
    data = json.dumps(dict(payload), ensure_ascii=True, separators=(",", ":"))
    return f"{_RIBBON_FN_JS}({data});"
