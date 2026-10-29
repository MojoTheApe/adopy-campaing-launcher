/* ACL-2292: "Recent runs" - the last 50 finished publish jobs, read from
   publish_history.jsonl through GET /publish-history. Survives a dashboard restart.
   One <script> tag in report.html; the button, panel and styles are made here.
   Every value from the server is escaped before it reaches innerHTML. */
(function () {
  'use strict';
  if (window.__aclRecentRuns) return;
  window.__aclRecentRuns = true;

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  var STYLE = [
    '#rr-pop{position:fixed;z-index:1000;right:12px;top:48px;width:min(560px,calc(100vw - 24px));max-height:70vh;overflow:auto;background:var(--panel,#fff);color:inherit;border:1px solid var(--border,#8886);border-radius:8px;padding:.6rem .8rem;box-shadow:0 6px 24px #0003;font-size:.8rem}',
    '#rr-pop[hidden]{display:none}',
    '#rr-pop h3{margin:0 0 .4rem;font-size:.9rem;display:flex;justify-content:space-between;align-items:center}',
    '.rr-run{border-top:1px solid var(--border,#8884);padding:.35rem 0}',
    '.rr-run small,.rr-msg{color:var(--muted,#888)}',
    '.rr-bad{color:var(--bad,#c0392b)}.rr-ok{color:var(--ok,#2a9d4b)}',
    '.rr-detail{white-space:pre-wrap;font-size:.72rem;margin:.3rem 0 0;max-height:240px;overflow:auto}'
  ].join('\n');

  var st = document.createElement('style');
  st.textContent = STYLE;
  document.head.appendChild(st);

  var anchor = document.getElementById('robot-health-link');
  var btn = document.createElement('a');
  btn.href = '#recent-runs';
  btn.id = 'recent-runs-link';
  btn.className = 'head-btn quiet';
  btn.textContent = 'Recent runs';
  btn.title = 'Results of the last 50 publish runs, kept across dashboard restarts';
  if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(btn, anchor);
  else { btn.style.cssText = 'position:fixed;right:12px;top:8px;z-index:999'; document.body.appendChild(btn); }

  var pop = document.createElement('div');
  pop.id = 'rr-pop';
  pop.hidden = true;
  pop.setAttribute('role', 'dialog');
  pop.setAttribute('aria-label', 'Recent runs');
  document.body.appendChild(pop);

  function when(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    return isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
  }
  function runHtml(r) {
    var cls = r.status === 'done' && r.publishable ? 'rr-ok' : 'rr-bad';
    var camps = (r.campaigns || []).map(function (c) {
      return esc(c.name) + ' (' + esc(c.status) + (c.campaign_id ? ', ' + esc(c.campaign_id) : '') + ')';
    }).join('; ');
    var errs = (r.errors || []).map(function (e) { return '<div class="rr-bad">' + esc(e) + '</div>'; }).join('');
    return '<div class="rr-run"><b>' + esc(r.account_label) + '</b> ' + esc(r.country)
      + ' <span class="' + cls + '">' + esc(r.status) + (r.publishable ? ', publishable' : '') + '</span>'
      + '<br><small>' + esc(when(r.started_at)) + ' - ' + esc(when(r.finished_at)) + (r.build ? ' - build ' + esc(r.build) : '') + '</small>'
      + '<div>' + camps + '</div>' + errs
      + '<div><a href="#" data-job="' + esc(r.job_id) + '">Show details</a></div>'
      + '<pre class="rr-detail" hidden></pre></div>';
  }
  function load() {
    pop.innerHTML = '<h3>Recent runs <a href="#" data-close="1">Close</a></h3><div class="rr-msg">Loading...</div>';
    fetch('/publish-history?limit=50', { headers: { 'Accept': 'application/json' } })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (d) {
        var runs = d.runs || [];
        pop.innerHTML = '<h3>Recent runs <a href="#" data-close="1">Close</a></h3>'
          + (runs.length ? runs.map(runHtml).join('') : '<div class="rr-msg">No finished runs have been recorded yet.</div>');
      })
      .catch(function () {
        pop.innerHTML = '<h3>Recent runs <a href="#" data-close="1">Close</a></h3><div class="rr-msg rr-bad">The history could not be read.</div>';
      });
  }
  function details(link) {
    var pre = link.parentNode.nextSibling;
    if (!pre.hidden) { pre.hidden = true; return; }
    pre.hidden = false;
    pre.textContent = 'Loading...';
    fetch('/publish-jobs/' + encodeURIComponent(link.getAttribute('data-job')))
      .then(function (r) { return r.json(); })
      .then(function (j) {
        var lines = (j.results || []).map(function (c) {
          return c.campaign_name + ' - ' + c.status + '\n  campaign ' + (c.campaign_id || '-') + ', ad set ' + (c.adset_id || '-')
            + ', ad ' + (c.ad_id || '-') + ', creative ' + (c.creative_id || '-')
            + (c.error ? '\n  error: ' + c.error : '')
            + ((c.blocking_failures || []).length ? '\n  blocking: ' + c.blocking_failures.join(' | ') : '')
            + ((c.warnings || []).length ? '\n  warnings: ' + c.warnings.join(' | ') : '')
            + ((c.steps || []).length ? '\n  steps: ' + c.steps.join(', ') : '');
        });
        pre.textContent = lines.join('\n') || (j.error || 'No campaign results.');
      })
      .catch(function () { pre.textContent = 'The run could not be read.'; });
  }
  function close() { pop.hidden = true; }
  btn.addEventListener('click', function (e) {
    e.preventDefault();
    if (pop.hidden) { pop.hidden = false; load(); } else close();
  });
  pop.addEventListener('click', function (e) {
    var t = e.target;
    if (!t || !t.getAttribute) return;
    if (t.getAttribute('data-close')) { e.preventDefault(); close(); }
    else if (t.getAttribute('data-job')) { e.preventDefault(); details(t); }
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });
})();
