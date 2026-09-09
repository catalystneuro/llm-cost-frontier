/* LLM cost frontier dashboard. Same charts as the blog post, fed from
   /data/llm-frontier.json, which a scheduled workflow refreshes from
   Artificial Analysis. */
(function () {
  'use strict';
  var BASE = location.pathname.replace(/[^/]*$/, '');
  var DATA_URL = BASE + 'data/llm-frontier.json';
  var DATA = null;

  var C = {
    surface: '#ffffff', grid: '#ecf1f8', axis: '#dfe6f1',
    ink: '#101642', ink2: '#55607a', muted: '#68718b', deemph: '#c2cbdc', retired: '#9aa4bb',
    snap: ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#4a3aa7', '#101642'],
    ord: ['#86b6ef', '#3987e5', '#1c5cab', '#0d366b']
  };
  var SNAPS = [];
  var TIERS = [];
  var CAPS = [];   // capability metric metadata from the data file
  var CAP = -1;    // active tab: -1 = overall Intelligence Index, else index into CAPS
  var ERAS = [];   // index era boundaries [date, note]; scores are only comparable within an era
  var ERA_VIEW = 0; // which era the Overall frontier chart shows; defaults to the current era

  var models = [], retiredByName = {}, openByName = {};
  function capMeta() { return CAP >= 0 ? CAPS[CAP] : null; }
  function curTiers() { return CAP < 0 ? TIERS : ((DATA.cap_tiers || {})[capMeta().key] || []); }
  function curTierCost() { return CAP < 0 ? DATA.tier_cost : ((DATA.cap_tier_cost || {})[capMeta().key] || {}); }
  function curTierSummary() { return CAP < 0 ? DATA.tier_summary : ((DATA.cap_tier_summary || {})[capMeta().key] || {}); }
  function score(m) { return CAP < 0 ? m.iq : (m.caps ? m.caps[CAP] : null); }
  function fmtScore(v) { var c = capMeta(); return v.toFixed(1) + (c && c.percent ? '%' : ''); }
  function metricName() { var c = capMeta(); return c ? c.metric : 'Index'; }
  function tierLabel(t) { var c = capMeta(); return '≥ ' + t + (c && c.percent ? '%' : ''); }
  // Cost in effect on a given date: the last recorded change at or before it.
  function costAt(m, date) {
    if (!m.hist) return m.mcost;
    var c = m.hist[0][1];
    for (var i = 0; i < m.hist.length; i++) { if (m.hist[i][0] <= date) c = m.hist[i][1]; else break; }
    return c;
  }
  // Index in effect on a given date, from the same change history.
  function iqAt(m, date) {
    if (!m.hist) return m.iq;
    var v = m.hist[0].length > 2 && m.hist[0][2] != null ? m.hist[0][2] : m.iq;
    for (var i = 0; i < m.hist.length; i++) {
      if (m.hist[i][0] > date) break;
      if (m.hist[i].length > 2 && m.hist[i][2] != null) v = m.hist[i][2];
    }
    return v;
  }
  function eraOfDate(date) {
    var n = 0;
    ERAS.forEach(function (e) { if (e[0] <= date) n++; });
    return n;
  }
  // Short display label for an era's index composition, from eras.json.
  function eraShortLabel(view) {
    if (!ERAS.length) return '';
    if (view >= ERAS.length) return ERAS[ERAS.length - 1][2] || '';
    return view === 0 ? (ERAS[0][3] || '') : (ERAS[view - 1][2] || '');
  }
  function dayBefore(iso) {
    var t = new Date(iso + 'T00:00:00Z');
    t.setUTCDate(t.getUTCDate() - 1);
    return t.toISOString().slice(0, 10);
  }
  // Cost on the current era's basis at any date. Within the current era it is
  // the cost measured then; before the recomposition it is today's cost
  // scaled by the model's own price ratio from its dated history, so price
  // changes carry over while suite changes never leak in.
  function basisCostAt(m, date) {
    if (!ERAS.length || eraOfDate(date) === ERAS.length) return costAt(m, date);
    var lastOld = costAt(m, dayBefore(ERAS[ERAS.length - 1][0]));
    return lastOld > 0 ? m.mcost * costAt(m, date) / lastOld : m.mcost;
  }
  // Whether a model had been measured under the era of snapDate by that date;
  // a model dropped at a boundary and re-measured later must not appear at
  // its old score in between.
  function measuredBy(m, snapDate) {
    var e = eraOfDate(snapDate);
    if (e === 0) return m.date <= snapDate;
    if (!m.hist) return m.date <= snapDate && eraOfDate(m.date) === e;
    for (var i = 0; i < m.hist.length; i++) if (m.hist[i][0] <= snapDate && eraOfDate(m.hist[i][0]) === e) return true;
    return false;
  }
  function loadData(d) {
    DATA = d;
    SNAPS = d.snapshots.map(function (s) { return [s[0], s[1]]; });
    TIERS = d.tiers.slice();
    CAPS = d.capabilities || [];
    ERAS = d.eras || [];
    ERA_VIEW = ERAS.length;
    models = d.models.map(function (m) {
      return { name: m[0], creator: m[1], date: m[2], iq: m[3], mcost: m[4], retired: !!m[5], open: !!m[6], hist: m[7] || null, caps: m[8] || null,
               era: m[9] != null ? m[9] : (d.eras || []).length };
    });
    retiredByName = {}; openByName = {};
    models.forEach(function (m) { retiredByName[m.name] = m.retired; openByName[m.name] = m.open; });
    anim.stage = SNAPS.length - 1;
  }

  function dot(svg, x, y, r, color, open) {
    if (open) svg.append(svgEl('circle', { cx: x, cy: y, r: r, fill: C.surface, stroke: color, 'stroke-width': 2 }));
    else svg.append(svgEl('circle', { cx: x, cy: y, r: r, fill: color, stroke: C.surface, 'stroke-width': 2 }));
  }

  function fmt$(c) { return '$' + (c >= 0.1 ? c.toFixed(2) : c >= 0.01 ? c.toFixed(3) : c.toFixed(4)); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function svgEl(tag, attrs) {
    var e = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }

  var tt = el('div', 'pfc-tooltip');
  document.body.appendChild(tt);

  function showTip(box, px, py, rows) {
    tt.replaceChildren.apply(tt, rows);
    tt.style.display = 'block';
    var r = box.getBoundingClientRect();
    var x = r.left + window.scrollX + px + 14;
    var y = r.top + window.scrollY + py - 10;
    tt.style.left = '0px'; tt.style.top = '0px';
    if (r.left + px + 14 + tt.offsetWidth > window.innerWidth - 8) x = r.left + window.scrollX + px - tt.offsetWidth - 14;
    tt.style.left = x + 'px'; tt.style.top = y + 'px';
  }
  function hideTip() { tt.style.display = 'none'; }

  function attachHover(box, svg, points) {
    svg.addEventListener('pointermove', function (ev) {
      var r = svg.getBoundingClientRect();
      var sx = svg.viewBox.baseVal.width / r.width;
      var mx = (ev.clientX - r.left) * sx, my = (ev.clientY - r.top) * sx;
      var best = null, bd = Infinity;
      var visible = points.filter(function (p) { return p.snap === undefined || p.snap <= anim.stage; });
      visible.forEach(function (p) {
        var d = Math.hypot(p.x - mx, p.y - my);
        if (d < bd) { bd = d; best = p; }
      });
      if (!best || bd > 40) { hideTip(); return; }
      var group = visible.filter(function (p) { return Math.hypot(p.x - best.x, p.y - best.y) < 3; });
      var rows = [], seen = {}, order = [];
      group.forEach(function (p) {
        var k = p.key || ('#' + p.x + ',' + p.y + Math.random());
        if (!seen[k]) { seen[k] = { pt: p, labels: [] }; order.push(k); }
        if (p.snapLabel) seen[k].labels.push(p.snapLabel);
        if (p.snapLabel && !seen[k].pt.snapLabel) seen[k].pt = p;
      });
      order.forEach(function (k) { rows = rows.concat(seen[k].pt.rows(seen[k].labels)); });
      showTip(box, best.x / sx, best.y / sx, rows);
    });
    svg.addEventListener('pointerleave', hideTip);
  }

  function frame(box, W, H, M, ariaLabel) {
    var svg = svgEl('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': ariaLabel });
    svg.style.display = 'block'; svg.style.width = '100%'; svg.style.height = 'auto';
    return svg;
  }

  // ---- chart 1: intelligence vs cost, frontier every two months ----
  var anim = { stage: SNAPS.length - 1, paused: true, timer: null, groups: [] };
  var STEP_MS = 1100, HOLD_MS = 6000;
  function applyStage() {
    anim.groups.forEach(function (gs, i) {
      gs.forEach(function (g) { g.setAttribute('display', i <= anim.stage ? 'inline' : 'none'); });
    });
    var cap = document.getElementById('pfc-frontier-stage');
    if (cap) cap.textContent = 'Pareto frontier as of ' + SNAPS[anim.stage][1];
  }
  function tick() {
    clearTimeout(anim.timer);
    if (anim.paused) return;
    anim.stage = anim.stage >= SNAPS.length - 1 ? 0 : anim.stage + 1;
    applyStage();
    hideTip();
    anim.timer = setTimeout(tick, anim.stage === SNAPS.length - 1 ? HOLD_MS : STEP_MS);
  }
  function setPaused(p) {
    anim.paused = p;
    var b = document.getElementById('pfc-frontier-toggle');
    if (b) { b.textContent = p ? 'Play' : 'Pause'; b.setAttribute('aria-pressed', p ? 'true' : 'false'); }
    if (p) clearTimeout(anim.timer);
    else anim.timer = setTimeout(tick, anim.stage === SNAPS.length - 1 ? HOLD_MS : STEP_MS);
  }

  function renderFrontier() {
    var box = document.getElementById('pfc-frontier');
    if (!box) return;
    box.replaceChildren();
    // The chart shows one index era at a time on every tab; the era toggle
    // above the figure picks which. Scores are only comparable within an era
    // on the Overall tab, and measured costs are only comparable within an
    // era on every tab, since a recomposition changes the evaluation suite.
    // Every value in the selected view is the value in effect at the era's
    // last day, so an old era is a frozen picture of how it ended.
    var eraView = DATA.era_snapshots ? ERA_VIEW : ERAS.length;
    SNAPS = (DATA.era_snapshots ? DATA.era_snapshots[eraView] : DATA.snapshots).map(function (s) { return [s[0], s[1]]; });
    if (anim.stage >= SNAPS.length) anim.stage = SNAPS.length - 1;
    anim.groups = SNAPS.map(function () { return []; });
    var viewEnd = SNAPS[SNAPS.length - 1][0];
    function viewScore(m) { return CAP < 0 ? iqAt(m, viewEnd) : score(m); }
    function viewCost(m) { return costAt(m, viewEnd); }
    var W = Math.max(320, Math.min(880, box.clientWidth)), H = 440;
    var M = { l: 56, r: 16, t: 12, b: 42 };
    var svg = frame(box, W, H, M, (CAP < 0 ? 'Intelligence Index' : metricName()) + ' versus cost per task with Pareto frontier lines every two months');
    // Models appear in an era's view only if they were measured in that era
    // (or later, for old views: their scores then are in the change history).
    var isCur = ERAS.length > 0 && eraView === ERAS.length;
    var ms = models.filter(function (m) {
      if (CAP >= 0 && score(m) == null) return false;
      if (isCur) return m.era === ERAS.length;
      return m.era >= eraView && measuredBy(m, viewEnd);
    });
    if (!ms.length) {
      box.append(el('p', 'pfc-lead', 'No current-era measurements for this metric yet.'));
      return;
    }
    var allS = ms.map(viewScore), allC = ms.map(viewCost);
    ms.forEach(function (m) {
      (m.hist || []).forEach(function (h) {
        if (h[0] > viewEnd) return;
        if (isCur) { allC.push(basisCostAt(m, h[0])); return; }
        if (eraOfDate(h[0]) !== eraView) return;
        allC.push(h[1]);
        if (CAP < 0 && h.length > 2 && h[2] != null) allS.push(h[2]);
      });
    });
    var maxS = Math.max.apply(null, allS);
    var minS = Math.min.apply(null, allS);
    var maxCost = Math.max.apply(null, allC);
    var minCost = Math.min.apply(null, allC);
    var yTop = CAP < 0 ? Math.max(66, Math.ceil((maxS + 3) / 10) * 10) : Math.min(100, Math.ceil((maxS + 3) / 10) * 10);
    var yBot = Math.max(minS < 0 ? -100 : 0, Math.floor((minS - 3) / 10) * 10);
    var xd = [minCost * 0.66, maxCost * 1.5], yd = [yBot, yTop];
    function X(v) { return M.l + (Math.log10(v) - Math.log10(xd[0])) / (Math.log10(xd[1]) - Math.log10(xd[0])) * (W - M.l - M.r); }
    function Y(v) { return H - M.b - (v - yd[0]) / (yd[1] - yd[0]) * (H - M.t - M.b); }

    var cs = []; for (var c0 = 0.001; c0 <= xd[1]; c0 *= 10) if (c0 >= xd[0]) cs.push(c0);
    cs.forEach(function (c) {
      svg.append(svgEl('line', { x1: X(c), x2: X(c), y1: M.t, y2: H - M.b, stroke: C.grid, 'stroke-width': 1 }));
      var lb = svgEl('text', { x: X(c), y: H - M.b + 18, 'text-anchor': 'middle', 'font-size': 11, fill: C.muted });
      lb.textContent = '$' + (c >= 1 ? c.toFixed(0) : c.toFixed(2)); svg.append(lb);
    });
    var qs = []; for (var q0 = yd[0]; q0 <= yd[1] - 5; q0 += 10) qs.push(q0);
    qs.forEach(function (q) {
      svg.append(svgEl('line', { x1: M.l, x2: W - M.r, y1: Y(q), y2: Y(q), stroke: C.grid, 'stroke-width': 1 }));
      var lb = svgEl('text', { x: M.l - 8, y: Y(q) + 4, 'text-anchor': 'end', 'font-size': 11, fill: C.muted });
      lb.textContent = q; svg.append(lb);
    });
    svg.append(svgEl('line', { x1: M.l, x2: W - M.r, y1: H - M.b, y2: H - M.b, stroke: C.axis, 'stroke-width': 1 }));
    svg.append(svgEl('line', { x1: M.l, x2: M.l, y1: M.t, y2: H - M.b, stroke: C.axis, 'stroke-width': 1 }));
    var xt = svgEl('text', { x: (M.l + W - M.r) / 2, y: H - 6, 'text-anchor': 'middle', 'font-size': 11.5, fill: C.ink2 });
    xt.textContent = 'Cost per task (log)'; svg.append(xt);
    var yt = svgEl('text', { x: 14, y: (M.t + H - M.b) / 2, 'font-size': 11.5, fill: C.ink2, transform: 'rotate(-90 14 ' + ((M.t + H - M.b) / 2) + ')', 'text-anchor': 'middle' });
    var verLbl = CAP < 0 ? eraShortLabel(eraView) : '';
    yt.textContent = CAP < 0 ? 'Artificial Analysis Intelligence Index' + (verLbl ? ' (' + verLbl + ')' : '') : metricName(); svg.append(yt);

    var pts = [];
    function windowIndex(date) {
      for (var i = 0; i < SNAPS.length; i++) if (date <= SNAPS[i][0]) return i;
      return SNAPS.length - 1;
    }
    // Colors count back from the dark end of the ramp so the newest frontier
    // is always the darkest, however few snapshots the era has.
    var cOff = Math.max(0, C.snap.length - SNAPS.length);
    function snapColor(i) { return C.snap[Math.min(C.snap.length - 1, i + cOff)]; }
    ms.forEach(function (m) {
      var sv = viewScore(m), cv = viewCost(m);
      var x = X(cv), y = Y(sv);
      var wi = windowIndex(m.date);
      var g = svgEl('g', { opacity: 0.45 });
      dot(g, x, y, 3.5, snapColor(wi), m.open);
      svg.append(g);
      anim.groups[wi].push(g);
      pts.push({ x: x, y: y, snap: wi, key: m.name, rows: function () {
        var d1 = el('div', 'pfc-tt-name'); d1.textContent = m.name;
        var d2 = el('div'); var s = el('span', 'pfc-tt-val'); s.textContent = fmt$(cv);
        d2.append(s, ' per task at ' + (CAP < 0 ? 'Index ' + sv.toFixed(1) : metricName() + ' ' + fmtScore(sv)));
        var d3 = el('div', null, m.creator + ' \u00b7 ' + (m.open ? 'open weights' : 'proprietary') + ' \u00b7 released ' + m.date + (m.retired ? ' \u00b7 retired' : ''));
        var d4 = el('div', 'pfc-tt-row'); var kd = el('span', 'pfc-tt-key'); kd.style.borderTopColor = snapColor(wi);
        d4.append(kd, 'in the ' + SNAPS[wi][1].replace('today', 'current') + ' window');
        return [d1, d2, d3, d4];
      }});
    });

    SNAPS.slice().reverse().forEach(function (snap, ri) {
      var i = SNAPS.length - 1 - ri;
      var snapDate = snap[0], snapLabel = snap[1];
      var est = isCur && eraOfDate(snapDate) < ERAS.length;
      var sub = ms.filter(function (m) {
        return isCur ? m.date <= snapDate : measuredBy(m, snapDate);
      }).map(function (m) {
        var c = isCur ? basisCostAt(m, snapDate) : costAt(m, snapDate);
        return { name: m.name, creator: m.creator, date: m.date, iq: CAP < 0 ? (isCur ? viewScore(m) : iqAt(m, snapDate)) : score(m), mcost: c, retired: m.retired, open: m.open, current: viewCost(m), est: est };
      });
      var fr = sub.filter(function (p) {
        return !sub.some(function (o) { return o.iq >= p.iq && o.mcost <= p.mcost && (o.iq > p.iq || o.mcost < p.mcost); });
      }).sort(function (a, b) { return a.iq - b.iq; });
      if (!fr.length) return;
      var color = snapColor(i);
      var d = 'M ' + X(fr[0].mcost) + ' ' + Y(fr[0].iq);
      fr.forEach(function (p, j) {
        if (j < fr.length - 1) d += ' H ' + X(fr[j + 1].mcost) + ' V ' + Y(fr[j + 1].iq);
      });
      d += ' H ' + (W - M.r);
      var sg = svgEl('g', {});
      sg.append(svgEl('path', { d: d, fill: 'none', stroke: color, 'stroke-width': i === SNAPS.length - 1 ? 3 : 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
      svg.append(sg);
      anim.groups[i].push(sg);
      fr.forEach(function (p) {
        var x = X(p.mcost), y = Y(p.iq);
        dot(sg, x, y, 4, color, p.open);
        pts.push({ x: x, y: y, snap: i, key: p.name, snapLabel: snapLabel, rows: function (labels) {
          var d1 = el('div', 'pfc-tt-name'); d1.textContent = p.name;
          var d2 = el('div', 'pfc-tt-row');
          var kd = el('span', 'pfc-tt-key'); kd.style.borderTopColor = color;
          var s = el('span', 'pfc-tt-val'); s.textContent = (p.est ? '≈' : '') + fmt$(p.mcost);
          d2.append(kd, s, ' at ' + (CAP < 0 ? 'Index ' + p.iq.toFixed(1) : metricName() + ' ' + fmtScore(p.iq)) + (Math.abs(p.current - p.mcost) > 1e-9 ? ' (price then; ' + fmt$(p.current) + (viewEnd === DATA.updated ? ' now)' : ' at era end)') : '') + (p.est ? ' · estimated from its price history' : ''));
          var d3 = el('div', null, 'Pareto frontier as of: ' + (labels && labels.length ? labels.join('; ') : snapLabel) + ' \u00b7 ' + (p.open ? 'open weights' : 'proprietary') + ' \u00b7 released ' + p.date + (p.retired ? ' \u00b7 retired' : ''));
          return [d1, d2, d3];
        }});
      });
    });
    box.append(svg);
    attachHover(box, svg, pts);
    var ctl = el('div', 'pfc-controls');
    var stageLabel = el('span', 'pfc-stage'); stageLabel.id = 'pfc-frontier-stage';
    var btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'pfc-btn'; btn.id = 'pfc-frontier-toggle';
    btn.addEventListener('click', function () { setPaused(!anim.paused); });
    btn.hidden = SNAPS.length < 2;  // a single-frontier era has nothing to animate
    ctl.append(stageLabel, btn);
    box.append(ctl);
    applyStage();
    setPaused(anim.paused);

    var legend = document.getElementById('pfc-frontier-legend');
    if (legend) {
      legend.replaceChildren();
      legend.append(el('span', 'pfc-legend-title', 'Pareto frontier as of'));
      SNAPS.slice().reverse().forEach(function (snap, ri) {
        var i = SNAPS.length - 1 - ri;
        var item = el('span', 'pfc-lk');
        var sw = el('span', 'pfc-swatch');
        sw.style.borderTopColor = snapColor(i);
        item.append(sw, el('span', null, snap[1]));
        legend.append(item);
      });
      var live = el('span', 'pfc-lk');
      var d1 = el('span', 'pfc-dot');
      d1.style.background = C.deemph; d1.style.borderColor = C.deemph;
      live.append(d1, el('span', null, 'proprietary'));
      var ret = el('span', 'pfc-lk');
      var d2 = el('span', 'pfc-dot');
      d2.style.background = C.surface; d2.style.borderColor = C.deemph;
      ret.append(d2, el('span', null, 'open weights'));
      legend.append(live, ret);
      if (verLbl) legend.append(el('span', 'pfc-legend-ver', 'AA Intelligence Index ' + verLbl));
    }
  }

  // ---- shareable view state in the URL hash ----
  function hashFor() {
    var key = CAP >= 0 ? CAPS[CAP].key : 'index';
    if (ERAS.length && ERA_VIEW < ERAS.length && DATA.era_snapshots) {
      var snaps = DATA.era_snapshots[ERA_VIEW];
      return '#' + key + '-through-' + snaps[snaps.length - 1][0];
    }
    return CAP >= 0 ? '#' + key : '';
  }
  function syncHash() {
    var h = hashFor();
    if ((location.hash || '') === h) return;
    history.replaceState(null, '', h || location.pathname + location.search);
  }
  function applyHash() {
    var h = (location.hash || '').replace(/^#/, '');
    if (!h || h === 'advances') return;
    for (var i = 0; i < CAPS.length; i++) if (CAPS[i].key === h) { CAP = i; return; }
    var m = h.match(/^([a-z]+)-through-(\d{4}-\d{2}-\d{2})$/);
    if (m && DATA.era_snapshots) {
      var cap = -1;
      if (m[1] !== 'index') {
        for (var k = 0; k < CAPS.length; k++) if (CAPS[k].key === m[1]) cap = k;
        if (cap === -1) return;
      }
      for (var e = 0; e < ERAS.length; e++) {
        var snaps = DATA.era_snapshots[e];
        if (snaps[snaps.length - 1][0] === m[2]) { CAP = cap; ERA_VIEW = e; return; }
      }
    }
  }

  // ---- the era archive link on the frontier chart ----
  // Past eras are an archive, not a peer view: every tab gets a quiet link to
  // each frozen era instead of a toggle. On Overall the score scale changed
  // there; on capability tabs the scores carry over but the cost basis does
  // not, so the earlier frontier history is an archive too.
  function switchEra(i) {
    ERA_VIEW = i;
    hideTip();
    anim.stage = 1e9;  // renderFrontier clamps to the view's last stage
    syncHash();
    renderEraNav(); renderFrontier(); renderRecords(); renderTable();
  }
  function renderEraNav() {
    var nav = document.getElementById('pfc-era-nav');
    if (!nav) return;
    var show = ERAS.length > 0 && DATA.era_snapshots;
    nav.hidden = !show;
    if (!show) return;
    nav.replaceChildren();
    for (var i = 0; i <= ERAS.length; i++) (function (i) {
      var b = document.createElement('button');
      b.type = 'button';
      var end = DATA.era_snapshots[i][DATA.era_snapshots[i].length - 1][0];
      var lbl = eraShortLabel(i);
      b.textContent = i === ERAS.length
        ? 'Index ' + (lbl || 'current') + (lbl ? ' (current)' : '')
        : 'Index ' + (lbl ? lbl + ' ' : '') + '(through ' + fmtDate(end) + ')';
      b.setAttribute('aria-pressed', ERA_VIEW === i ? 'true' : 'false');
      b.addEventListener('click', function () {
        if (ERA_VIEW !== i) switchEra(i);
      });
      nav.append(b);
    })(i);
  }

  // ---- capability tabs ----
  var leadDefault = null;
  function renderTabs() {
    var bar = document.getElementById('pfc-cap-tabs');
    if (!bar || !CAPS.length) return;
    bar.replaceChildren();
    var tabs = [['Overall', -1]].concat(CAPS.map(function (c, i) { return [c.label, i]; }));
    tabs.forEach(function (t) {
      var b = document.createElement('button');
      b.type = 'button'; b.className = 'pfc-tab'; b.textContent = t[0];
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-selected', CAP === t[1] ? 'true' : 'false');
      b.addEventListener('click', function () {
        if (CAP === t[1]) return;
        CAP = t[1];
        advPage = 1;
        ERA_VIEW = ERAS.length;
        hideTip();
        anim.stage = 1e9;
        syncHash();
        renderTabs(); renderEraNav(); renderLead(); renderFrontier(); renderCapTable(); renderRecords(); renderTable(); renderAdvances();
      });
      bar.append(b);
    });
  }
  function renderLead() {
    var p = document.getElementById('pfc-frontier-lead');
    if (!p) return;
    if (leadDefault === null) leadDefault = p.innerHTML;
    var c = capMeta();
    if (!c) { p.innerHTML = leadDefault; return; }
    var n = models.filter(function (m) { return score(m) != null; }).length;
    p.textContent = c.blurb + ' Measured for ' + n + ' of ' + models.length + ' tracked models.';
  }
  function renderCapTable() {
    var wrap = document.getElementById('pfc-cap-table-wrap');
    if (!wrap) return;
    var c = capMeta();
    if (!c) { wrap.hidden = true; return; }
    wrap.hidden = false;
    var th = document.getElementById('pfc-cap-metric-name');
    if (th) th.textContent = c.metric;
    var tb = document.getElementById('pfc-cap-table');
    if (!tb) return;
    tb.replaceChildren();
    var live = models.filter(function (m) { return !m.retired && score(m) != null && m.era === ERAS.length; });
    if (!live.length) return;
    var maxS = Math.max.apply(null, live.map(score));
    var hi = Math.floor(maxS / 10) * 10;
    function cell(node, cls) { var td = document.createElement('td'); if (cls) td.className = cls; td.append(node); return td; }
    [hi, hi - 10, hi - 20, hi - 30].forEach(function (t) {
      if (t <= 0) return;
      var cands = live.filter(function (m) { return score(m) >= t; });
      if (!cands.length) return;
      var best = cands.reduce(function (a, b) { return b.mcost < a.mcost ? b : a; });
      var tr = document.createElement('tr');
      tr.append(cell('≥ ' + t + (c.percent ? '%' : ''), 'pfc-td-tier'));
      var w = el('div', 'pfc-ev');
      var a1 = el('div', 'pfc-ev-top'); a1.textContent = best.name;
      var a2 = el('div', 'pfc-ev-model'); a2.textContent = best.creator + ' · ' + (best.open ? 'open weights' : 'proprietary');
      w.append(a1, a2);
      tr.append(cell(w));
      tr.append(cell(fmt$(best.mcost), 'pfc-td-num'));
      tr.append(cell(fmtScore(score(best)), 'pfc-td-num'));
      tb.append(tr);
    });
  }

  // ---- chart 2: cost records by tier ----
  function renderRecords() {
    var box = document.getElementById('pfc-records');
    if (!box) return;
    box.replaceChildren();
    var W = Math.max(320, Math.min(880, box.clientWidth)), H = 370;
    var M = { l: 56, r: 60, t: 12, b: 40 };
    // The era toggle controls this chart too. The current view is one
    // continuous series rebased onto the current index: current scores, with
    // pre-recomposition costs from each model's own price ratios. An archive
    // view shows only that era's actual measured records.
    var isCur = ERAS.length > 0 && ERA_VIEW === ERAS.length;
    var isArch = ERAS.length > 0 && ERA_VIEW < ERAS.length;
    var lead = document.getElementById('pfc-records-lead');
    if (lead) {
      lead.textContent = 'The cheapest cost per task achieved by any released model at or above each ' + (CAP < 0 ? 'Intelligence Index' : metricName()) + ' tier, by release date. Each step is a model that set a new low for its tier.' + (isCur ? ' Costs before the recomposition are estimated from each model\'s own price history on the current basis.' : '');
    }
    var svg = frame(box, W, H, M, 'Running minimum cost per task by capability tier');
    var tiers = curTiers();
    var tierCost = curTierCost();
    if (isCur && DATA.tier_cost_rebased) {
      tierCost = CAP < 0 ? DATA.tier_cost_rebased : ((DATA.cap_tier_cost_rebased || {})[capMeta().key] || tierCost);
    }
    if (isArch) {
      var clipped = {};
      tiers.forEach(function (t) {
        clipped[t] = (tierCost[t] || []).filter(function (r) { return eraOfDate(r[0]) === ERA_VIEW; });
      });
      tierCost = clipped;
    }
    var endDate = isArch ? DATA.era_snapshots[ERA_VIEW][DATA.era_snapshots[ERA_VIEW].length - 1][0] : DATA.updated;
    var allRecs = []; tiers.forEach(function (t) { (tierCost[t] || []).forEach(function (r) { allRecs.push(r); }); });
    if (!allRecs.length) { box.append(el('p', 'pfc-lead', 'No records in this era.')); return; }
    // Fixed time origin so the axis reads the same on every tab; records set
    // before it enter from the left edge at the value in effect on that date.
    var X0DATE = '2025-08-01';
    var x0d = new Date(X0DATE + 'T00:00:00Z');
    var x1d = new Date(endDate + 'T00:00:00Z');
    var x0 = x0d.getTime(), x1 = x1d.getTime();
    var maxRec = Math.max.apply(null, allRecs.map(function (r) { return r[1]; }));
    var yd = [0.005, Math.max(5, Math.pow(10, Math.ceil(Math.log10(maxRec))))];
    var ticks = []; var td = new Date(x0d.getTime());
    var monthsSpan = (x1d.getUTCFullYear() - x0d.getUTCFullYear()) * 12 + x1d.getUTCMonth() - x0d.getUTCMonth();
    var stepM = monthsSpan > 30 ? 6 : monthsSpan > 16 ? 3 : 2;
    var MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    while (td.getTime() <= x1) { ticks.push([td.toISOString().slice(0, 10), MON[td.getUTCMonth()] + " '" + String(td.getUTCFullYear()).slice(2)]); td.setUTCMonth(td.getUTCMonth() + stepM); }
    function X(dstr) { return M.l + (Date.parse(dstr + 'T00:00:00Z') - x0) / (x1 - x0) * (W - M.l - M.r); }
    function Y(v) { return H - M.b - (Math.log10(v) - Math.log10(yd[0])) / (Math.log10(yd[1]) - Math.log10(yd[0])) * (H - M.t - M.b); }

    ticks.forEach(function (t) {
      svg.append(svgEl('line', { x1: X(t[0]), x2: X(t[0]), y1: M.t, y2: H - M.b, stroke: C.grid, 'stroke-width': 1 }));
      var lb = svgEl('text', { x: X(t[0]), y: H - M.b + 18, 'text-anchor': 'middle', 'font-size': 11, fill: C.muted });
      lb.textContent = t[1]; svg.append(lb);
    });
    var vs = []; for (var v0 = 0.01; v0 <= yd[1] / 2; v0 *= 10) vs.push(v0);
    vs.forEach(function (v) {
      svg.append(svgEl('line', { x1: M.l, x2: W - M.r, y1: Y(v), y2: Y(v), stroke: C.grid, 'stroke-width': 1 }));
      var lb = svgEl('text', { x: M.l - 8, y: Y(v) + 4, 'text-anchor': 'end', 'font-size': 11, fill: C.muted });
      lb.textContent = '$' + (v >= 1 ? v.toFixed(0) : v.toFixed(2)); svg.append(lb);
    });
    svg.append(svgEl('line', { x1: M.l, x2: W - M.r, y1: H - M.b, y2: H - M.b, stroke: C.axis, 'stroke-width': 1 }));
    svg.append(svgEl('line', { x1: M.l, x2: M.l, y1: M.t, y2: H - M.b, stroke: C.axis, 'stroke-width': 1 }));

    var pts = [];
    var endLabels = [];
    // Every view of this chart is on a single index basis, named at the top:
    // the rebased current basis, or the archived era's actual one.
    var eraStarts = [];
    function eraCapX() { return W - M.r; }
    if (ERAS.length) {
      var lblv = eraShortLabel(ERA_VIEW);
      if (lblv) {
        var lb = svgEl('text', { x: (M.l + W - M.r) / 2, y: M.t + 11, 'text-anchor': 'middle', 'font-size': 10, fill: C.ink2 });
        lb.textContent = 'index ' + lblv + (isCur ? ' basis' : '');
        svg.append(lb);
      }
    }
    tiers.forEach(function (tier, i) {
      var all = tierCost[tier];
      if (!all || !all.length) return;
      var carry = null;
      var recs = all.filter(function (r) { if (r[0] < X0DATE) { carry = r; return false; } return true; });
      if (!carry && !recs.length) return;
      var color = C.ord[i];
      var d = '';
      if (carry) {
        d = 'M ' + M.l + ' ' + Y(carry[1]) + ' H ' + (recs.length ? X(recs[0][0]) : W - M.r);
      }
      recs.forEach(function (r, j) {
        var x = X(r[0]), y = Y(r[1]);
        d += (d ? ' V ' + y : ' M ' + x + ' ' + y);
        var next = j < recs.length - 1 ? recs[j + 1] : null;
        d += ' H ' + (next ? X(next[0]) : W - M.r);
      });
      svg.append(svgEl('path', { d: d, fill: 'none', stroke: color, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
      recs.forEach(function (r) {
        var x = X(r[0]), y = Y(r[1]);
        dot(svg, x, y, 4, color, !!openByName[r[2]]);
        pts.push({ x: x, y: y, rows: function () {
          var d1 = el('div', 'pfc-tt-name'); d1.textContent = r[2];
          var d2 = el('div', 'pfc-tt-row');
          var kd = el('span', 'pfc-tt-key'); kd.style.borderTopColor = color;
          var est = isCur && ERAS.length && r[0] < ERAS[ERAS.length - 1][0];
          var s = el('span', 'pfc-tt-val'); s.textContent = (est ? '≈' : '') + fmt$(r[1]);
          d2.append(kd, s, ' new record, ' + (CAP < 0 ? 'Index' : metricName()) + ' ' + tierLabel(tier) + (est ? ' · estimated from price history' : ''));
          var d3 = el('div', null, (r[4] ? r[4] + ' \u00b7 ' : 'released ' + r[0] + ' \u00b7 ') + (CAP < 0 ? 'Index ' + r[3].toFixed(1) : metricName() + ' ' + fmtScore(r[3])) + (openByName[r[2]] ? ' \u00b7 open weights' : ' \u00b7 proprietary') + (retiredByName[r[2]] ? ' \u00b7 retired' : ''));
          return [d1, d2, d3];
        }});
      });
      var endY = Y((recs.length ? recs[recs.length - 1] : carry)[1]);
      if (!endLabels.some(function (yy) { return Math.abs(yy - endY) < 14; })) {
        var lb = svgEl('text', { x: W - M.r + 6, y: endY + 4, 'font-size': 10.5, 'font-weight': 500, fill: C.ink2 });
        lb.textContent = tierLabel(tier); svg.append(lb);
        endLabels.push(endY);
      }
    });
    box.append(svg);
    attachHover(box, svg, pts);

    var legend = document.getElementById('pfc-records-legend');
    if (legend) {
      legend.replaceChildren();
      legend.append(el('span', 'pfc-legend-title', CAP < 0 ? 'Intelligence Index' : metricName()));
      tiers.slice().reverse().forEach(function (tier, ri) {
        var i = tiers.length - 1 - ri;
        var item = el('span', 'pfc-lk');
        var sw = el('span', 'pfc-swatch');
        sw.style.borderTopColor = C.ord[i];
        item.append(sw, el('span', null, tierLabel(tier)));
        legend.append(item);
      });
    }
  }

  function fmtDate(d) { var p = d.split('-'); var MON = ['January','February','March','April','May','June','July','August','September','October','November','December']; return MON[+p[1] - 1] + ' ' + (+p[2]) + ', ' + p[0]; }
  function renderTable() {
    var tb = document.getElementById('pfc-tier-table');
    if (!tb) return;
    tb.replaceChildren();
    function cell(html, cls) { var td = document.createElement('td'); if (cls) td.className = cls; td.append(html); return td; }
    function event(date, model, cost) {
      var w = el('div', 'pfc-ev');
      var a = el('div', 'pfc-ev-top'); var c = el('span', 'pfc-ev-cost'); c.textContent = fmt$(cost); a.append(c, ' \u00b7 ' + fmtDate(date));
      var b = el('div', 'pfc-ev-model'); b.textContent = model;
      w.append(a, b); return w;
    }
    var isCur = ERAS.length > 0 && ERA_VIEW === ERAS.length;
    var isArch = ERAS.length > 0 && ERA_VIEW < ERAS.length;
    var summary = curTierSummary();
    if (isCur && DATA.tier_summary_rebased) {
      summary = CAP < 0 ? DATA.tier_summary_rebased : ((DATA.cap_tier_summary_rebased || {})[capMeta().key] || summary);
    }
    if (isArch) {
      // Summaries for an archived era come from that era's actual records.
      var tc = curTierCost();
      summary = {};
      curTiers().forEach(function (t) {
        var recs = (tc[t] || []).filter(function (r) { return eraOfDate(r[0]) === ERA_VIEW; });
        if (!recs.length) { summary[t] = null; return; }
        var f = recs[0], l = recs[recs.length - 1];
        var days = (Date.parse(l[0]) - Date.parse(f[0])) / 86400000;
        var ratio = f[1] / l[1];
        summary[t] = { first_date: f[0], first_model: f[2], first_cost: f[1],
                       last_date: l[0], last_model: l[2], last_cost: l[1],
                       collapse: Math.round(ratio * 10) / 10,
                       halving_days: ratio > 1 && days ? Math.round(days / (Math.log(ratio) / Math.LN2)) : null };
      });
    }
    curTiers().forEach(function (t) {
      var s = summary[t];
      var tr = document.createElement('tr');
      tr.append(cell(tierLabel(t), 'pfc-td-tier'));
      if (s) {
        tr.append(cell(event(s.first_date, s.first_model, s.first_cost)));
        tr.append(cell(event(s.last_date, s.last_model, s.last_cost)));
        tr.append(cell(s.collapse + 'x', 'pfc-td-num'));
        tr.append(cell(s.halving_days ? '~' + s.halving_days + ' days' : '', 'pfc-td-num'));
      } else {
        var td = cell('not yet reached'); td.colSpan = 4; tr.append(td);
      }
      tb.append(tr);
    });
    document.querySelectorAll('.pfc-updated').forEach(function (e) { e.textContent = fmtDate(DATA.updated); });
    document.querySelectorAll('.pfc-count').forEach(function (e) { e.textContent = String(DATA.counts.total); });
  }
  var ADV_DAYS_PER_PAGE = 6;
  var advPage = 1;
  function joinAnd(items) {
    if (items.length <= 1) return items.join('');
    if (items.length === 2) return items[0] + ' and ' + items[1];
    return items.slice(0, -1).join(', ') + ', and ' + items[items.length - 1];
  }
  function takenClause(taken, departed) {
    var out = '';
    if (taken.length) {
      out += ', taking it from ' + joinAnd(taken);
      var gone = taken.filter(function (t) { return departed.indexOf(t) >= 0; });
      if (gone.length && gone.length === taken.length) out += taken.length === 1 ? ', which left the frontier' : taken.length === 2 ? ', both of which left the frontier' : ', all of which left the frontier';
      else if (gone.length) out += '; ' + joinAnd(gone) + ' left the frontier';
    }
    var extra = departed.filter(function (d) { return taken.indexOf(d) < 0; });
    if (extra.length) out += '; ' + joinAnd(extra) + ' left the frontier';
    return out;
  }
  function advanceLine(a, withVariant) {
    var text = el('div', 'pfc-adv-body');
    if (withVariant && a.variant) { var v = el('span', 'pfc-adv-variant'); v.textContent = a.variant + ': '; text.append(v); }
    var term = CAP < 0 ? 'index' : metricName();
    var span = a.owns_to.toFixed(1) === a.owns_from.toFixed(1) ? term + ' ' + fmtScore(a.owns_to) : term + ' ' + fmtScore(a.owns_from) + ' to ' + fmtScore(a.owns_to);
    var ceiling = CAP < 0 ? 'the intelligence ceiling' : 'the ' + metricName() + ' ceiling';
    var s1 = a.kind === 'price change' && a.previous_cost
      ? 'price moved from ' + fmt$(a.previous_cost) + ' to ' + fmt$(a.cost_per_task) + ' per task; now the cheapest way to reach ' + span
      : (a.ceiling_from !== null && a.ceiling_from !== undefined)
        ? 'pushed ' + ceiling + ' from ' + fmtScore(a.ceiling_from) + ' to ' + fmtScore(a.owns_to) + ', at ' + fmt$(a.cost_per_task) + ' per task'
        : 'now the cheapest way to reach ' + span + ' at ' + fmt$(a.cost_per_task) + ' per task';
    s1 += takenClause(a.taken_from || [], a.displaced || []) + '. ';
    if (!(withVariant && a.variant)) s1 = s1.charAt(0).toUpperCase() + s1.slice(1);
    text.append(s1);
    if (a.records && a.records.length) { var r = el('span', 'pfc-adv-rec'); r.textContent = 'New cost record for ' + joinAnd(a.records.map(function (t) { return (CAP < 0 ? 'index' : metricName()) + ' ' + tierLabel(t); })) + '. '; text.append(r); }
    return text;
  }
  function slugify(name) {
    return name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  }
  function advanceGroup(list) {
    // list: advances for one base model on one day, highest index first
    var item = el('div', 'pfc-adv-item');
    var single = list.length === 1 && !list[0].variant;
    var head = el('div', 'pfc-adv-head'); head.textContent = single ? list[0].model : list[0].base;
    var kinds = []; list.forEach(function (a) { if (kinds.indexOf(a.kind) < 0) kinds.push(a.kind); });
    kinds.forEach(function (k) { head.append(el('span', 'pfc-adv-kind', k)); });
    if (list[0].open_weights) head.append(el('span', 'pfc-adv-kind pfc-adv-open', 'open weights'));
    // The pipeline renders one shareable card image per base model per day,
    // named by the same date and slug this derives; capability advances get
    // their cards under a per-metric subdirectory.
    var card = document.createElement('a');
    card.className = 'pfc-adv-kind pfc-adv-card';
    card.textContent = 'chart card';
    card.href = BASE + 'images/advances/' + (CAP < 0 ? '' : capMeta().key + '/') + list[0].date + '-' + slugify(list[0].base || list[0].model) + '.png';
    card.target = '_blank';
    card.rel = 'noopener';
    head.append(card);
    item.append(head);
    list.forEach(function (a) { item.append(advanceLine(a, !single)); });
    return item;
  }
  var advLeadDefault = null;
  function renderAdvances() {
    var box = document.getElementById('pfc-advances');
    if (!box || !DATA.advances) return;
    var lead = document.getElementById('pfc-adv-lead');
    if (lead) {
      if (advLeadDefault === null) advLeadDefault = lead.innerHTML;
      if (CAP < 0) lead.innerHTML = advLeadDefault;
      else lead.textContent = 'Each entry is a date on which a model became the cheapest way to reach some level of ' + metricName() + ', through a release or a price change, derived from release dates, observed prices, and the latest measured scores. The Atom feed covers the Overall view only.';
    }
    var list = CAP < 0 ? DATA.advances : ((DATA.cap_advances || {})[capMeta().key] || []);
    var days = [], byDay = {};
    list.forEach(function (a) {
      if (!byDay[a.date]) { byDay[a.date] = []; days.push(a.date); }
      byDay[a.date].push(a);
    });
    var total = Math.max(1, Math.ceil(days.length / ADV_DAYS_PER_PAGE));
    advPage = Math.min(Math.max(1, advPage), total);
    box.replaceChildren();
    if (!days.length) box.append(el('p', 'pfc-lead', 'No advances recorded for this metric.'));
    days.slice((advPage - 1) * ADV_DAYS_PER_PAGE, advPage * ADV_DAYS_PER_PAGE).forEach(function (d) {
      var row = el('div', 'pfc-adv-day');
      var col = el('div');
      var groups = [], byBase = {};
      byDay[d].forEach(function (a) {
        var key = a.base || a.model;
        if (!byBase[key]) { byBase[key] = []; groups.push(key); }
        byBase[key].push(a);
      });
      groups.forEach(function (k) { col.append(advanceGroup(byBase[k])); });
      row.append(el('div', 'pfc-adv-date', fmtDate(d)), col);
      box.append(row);
    });
    var pager = document.getElementById('pfc-adv-pager'), prev = document.getElementById('pfc-adv-prev'), next = document.getElementById('pfc-adv-next'), lab = document.getElementById('pfc-adv-page');
    if (pager) {
      pager.hidden = total <= 1;
      prev.disabled = advPage === 1; next.disabled = advPage === total;
      lab.textContent = 'Page ' + advPage + ' of ' + total;
      if (!pager.dataset.wired) {
        pager.dataset.wired = '1';
        prev.addEventListener('click', function () { advPage -= 1; renderAdvances(); document.getElementById('advances').scrollIntoView({ behavior: 'smooth', block: 'start' }); });
        next.addEventListener('click', function () { advPage += 1; renderAdvances(); document.getElementById('advances').scrollIntoView({ behavior: 'smooth', block: 'start' }); });
      }
    }
  }
  function renderAll() { if (!DATA) return; renderTabs(); renderEraNav(); renderLead(); renderFrontier(); renderCapTable(); renderRecords(); renderTable(); renderAdvances(); }
  fetch(DATA_URL, { cache: 'no-cache' }).then(function (r) { return r.json(); }).then(function (d) {
    loadData(d);
    applyHash();
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', renderAll);
    else renderAll();
  }).catch(function (err) {
    var box = document.getElementById('pfc-frontier');
    if (box) box.textContent = 'Could not load the frontier data (' + err + ').';
  });
  var rt = null;
  window.addEventListener('resize', function () { clearTimeout(rt); rt = setTimeout(renderAll, 150); });
})();
