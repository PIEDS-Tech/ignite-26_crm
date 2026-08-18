/* Quiet background refresh, shared by the Django CRM and the local agent.
 *
 * One master database now has fifteen people writing to it from two different
 * apps, so an open tab goes stale within seconds of someone else's edit. This
 * re-fetches on a timer and on focus.
 *
 * Deliberately not websockets: that would mean a second auth system and a
 * Supabase key in every browser, to save nineteen seconds.
 *
 * Usage (Django pages):
 *     <div data-poll> ... </div>
 *     Poll.page();
 *
 * Usage (agent, or any custom refresh):
 *     Poll.every(loadContacts);
 */
(function (global) {
  'use strict';

  const INTERVAL_MS = 20000;

  //: Set by callers around work that must not be interrupted -- a send in
  //: progress, or an open edit dialog whose fields would be wiped.
  let paused = 0;
  let lastRefresh = Date.now();

  function pause()  { paused += 1; }
  function resume() { paused = Math.max(0, paused - 1); }
  function isPaused() { return paused > 0; }

  function stamp() {
    lastRefresh = Date.now();
    document.querySelectorAll('[data-poll-stamp]').forEach(el => {
      el.dataset.ts = String(lastRefresh);
    });
  }

  /* "updated 12s ago", ticking once a second so a frozen tab looks frozen. */
  function startClock() {
    setInterval(() => {
      const secs = Math.round((Date.now() - lastRefresh) / 1000);
      document.querySelectorAll('[data-poll-stamp]').forEach(el => {
        el.textContent = secs < 5 ? 'up to date' : `updated ${secs}s ago`;
      });
    }, 1000);
  }

  /** Run `fn` every 20s, on tab focus, and never while paused. */
  function every(fn) {
    async function tick() {
      if (isPaused() || document.hidden) return;
      try {
        await fn();
        stamp();
      } catch (e) {
        /* A failed poll is not worth interrupting anyone over -- the next one
           in 20s will most likely succeed, and any real action the user takes
           surfaces the error properly. */
        console.warn('[poll]', e);
      }
    }

    setInterval(tick, INTERVAL_MS);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) tick();
    });
    startClock();
    return tick;
  }

  /* Values of the checked row boxes. A "select all" header box carries no
     value of its own (the browser defaults it to "on"), so it is ignored --
     otherwise leaving it ticked with no rows selected would block refreshes
     for as long as the tab stayed open. */
  function checkedValues() {
    return Array.from(
      document.querySelectorAll('[data-poll] input[type=checkbox]:checked')
    ).map(c => c.value).filter(v => v && v !== 'on');
  }

  /* Re-fetch this same URL and swap the [data-poll] regions.
   *
   * Checkbox state is preserved across the swap: losing a 40-contact selection
   * to a background refresh would make the feature worse than not having it.
   *
   * Two separate things protect it, because one was not enough:
   *
   *   1. A refresh is SKIPPED outright while anything is selected. Someone
   *      part-way through picking 40 contacts does not want the table moving
   *      under them, and other people's edits can wait until they submit.
   *   2. If a refresh does land, the checked set is re-read immediately before
   *      the swap as well as before the fetch, and the two are merged.
   *
   * (2) is the one that actually bit: the snapshot used to be taken before a
   * fetch that could run for ten seconds on a slow page, so every box ticked
   * while it was in flight was silently rolled back on arrival. It looked
   * exactly like a cap on how many contacts you were allowed to select. */
  async function refreshPage() {
    if (checkedValues().length) return;

    const checked = new Set(checkedValues());

    const html = await fetch(location.href, {
      headers: { 'X-Requested-With': 'poll' },
      credentials: 'same-origin',
    }).then(r => {
      if (!r.ok) throw new Error(r.status);
      return r.text();
    });

    const doc = new DOMParser().parseFromString(html, 'text/html');
    const fresh = doc.querySelectorAll('[data-poll]');
    const here = document.querySelectorAll('[data-poll]');
    if (fresh.length !== here.length) return;   // page shape changed; leave it alone

    // Anything ticked while the fetch was in flight was ticked on the DOM we
    // are about to destroy. Collect it before the swap, not after.
    checkedValues().forEach(v => checked.add(v));

    here.forEach((el, i) => { el.innerHTML = fresh[i].innerHTML; });

    document.querySelectorAll('[data-poll] input[type=checkbox]').forEach(c => {
      if (checked.has(c.value)) c.checked = true;
    });
    document.dispatchEvent(new CustomEvent('poll:refreshed'));
  }

  function page() {
    if (!document.querySelector('[data-poll]')) return;
    return every(refreshPage);
  }

  global.Poll = { every, page, pause, resume, isPaused, refreshPage };
})(window);
