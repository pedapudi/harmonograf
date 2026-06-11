/* ===================================================================
   EMBED MODE — shared across the harmonograf zicato-UI-study pages.
   Ported from zicato tournament-viz-study/_embed.js. Honors URL params
   so a page can be composed into compose.html via an <iframe>. The
   NORMAL (no-param) view is left 100% unchanged: this script no-ops
   unless ?only / ?bare / ?theme is present.

   Contract:
     ?only=N   (1-based)  render ONLY section N (hide the others)
     ?bare=1              strip page chrome (header/footer/intro + each
                          section's title/rationale/legend/caption),
                          leaving just the section's primary figure(s).
     ?theme=NAME          set <html data-theme=NAME> at load (16 themes)

   Per-page config in window.__EMBED_CFG__ (set just before this runs):
     optSel    CSS selector for the SURFACE sections, in order.
     bareHide  [selectors] hidden inside a kept section in bare mode
               (titles / rationales / legends / captions / before-panels).
   =================================================================== */
(function () {
  var P = new URLSearchParams(location.search);
  var only = P.get('only');
  var bare = P.get('bare') === '1' || P.get('bare') === 'true';
  var theme = P.get('theme');
  if (only == null && !bare && !theme) return; // normal view untouched

  var CFG = window.__EMBED_CFG__ || {};
  var THEME_IDS = (window.THEME_IDS) || [];

  function apply() {
    if (theme && (!THEME_IDS.length || THEME_IDS.indexOf(theme) >= 0)) {
      document.documentElement.setAttribute('data-theme', theme);
    }
    document.documentElement.classList.add('embed');
    if (bare) document.documentElement.classList.add('embed-bare');

    var opts = CFG.optSel ? Array.prototype.slice.call(document.querySelectorAll(CFG.optSel)) : [];

    var keepIdx = null;
    if (only != null) {
      keepIdx = parseInt(only, 10) - 1;
      opts.forEach(function (o, i) { if (i !== keepIdx) o.style.display = 'none'; });
    }
    var kept = (keepIdx != null && opts[keepIdx]) ? [opts[keepIdx]]
             : (opts.length ? opts : []);

    if (!bare) return;

    var header = document.querySelector('header');
    if (header) header.style.display = 'none';
    var footer = document.querySelector('footer');
    if (footer) footer.style.display = 'none';

    var main = document.querySelector('main');
    var keepSet = kept;
    if (main) {
      Array.prototype.slice.call(main.children).forEach(function (child) {
        if (keepSet.indexOf(child) >= 0) return;
        if (child.querySelector && keepSet.some(function (k) { return child.contains(k); })) return;
        child.style.display = 'none';
      });
    }
    document.body.style.padding = '0';
    document.body.style.margin = '0';

    (CFG.bareHide || []).forEach(function (sel) {
      kept.forEach(function (k) {
        Array.prototype.slice.call(k.querySelectorAll(sel)).forEach(function (n) { n.style.display = 'none'; });
      });
    });
    kept.forEach(function (k) {
      k.style.margin = '0';
      k.style.border = 'none';
      k.style.background = 'transparent';
      k.style.padding = '6px 14px';
    });
  }

  /* report content height to the composer so it can size the iframe */
  function reportHeight() {
    try {
      var h = Math.max(
        document.body.scrollHeight, document.documentElement.scrollHeight,
        document.body.offsetHeight, document.documentElement.offsetHeight
      );
      parent.postMessage({ __hgEmbedHeight: h, key: P.get('lvl') || null }, '*');
    } catch (e) {}
  }

  function run() {
    apply();
    requestAnimationFrame(function () { requestAnimationFrame(reportHeight); });
    setTimeout(reportHeight, 120);
    setTimeout(reportHeight, 400);
    window.addEventListener('resize', reportHeight);
  }

  if (document.readyState === 'complete' || document.readyState === 'interactive') run();
  else document.addEventListener('DOMContentLoaded', run);
})();
