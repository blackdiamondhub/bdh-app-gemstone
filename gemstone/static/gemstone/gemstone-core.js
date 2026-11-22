/* Gemstone Lights — pure helpers shared by the UI and its Node tests (tests/js). No DOM. */
(function (root) {
  'use strict';

  // Scale and offset that show every point (photo coordinates, 0..1 of the
  // picture's width/height) inside a boxW×boxH box. The points' bounding box,
  // padded by ``pad`` of its larger side, is fitted whole (contain) and centred;
  // the rest of the box is letterboxed. ``aspect`` is the picture's width/height.
  // Returns { s, pw, ph, ox, oy }: the picture is drawn pw×ph at (ox, oy).
  function fitBox(points, aspect, boxW, boxH, pad) {
    const A = aspect > 0 ? aspect : 2;
    pad = pad == null ? 0.08 : pad;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    (points || []).forEach((p) => {
      x0 = Math.min(x0, p[0]); x1 = Math.max(x1, p[0]);
      y0 = Math.min(y0, p[1]); y1 = Math.max(y1, p[1]);
    });
    if (!(x1 >= x0)) { x0 = 0; x1 = 1; y0 = 0; y1 = 1; pad = 0; }
    // Bounding box in units of the picture's height.
    const bw = (x1 - x0) * A, bh = y1 - y0;
    const m = pad * Math.max(bw, bh, 0.05);
    const s = Math.min(boxW / (bw + 2 * m), boxH / (bh + 2 * m));
    const pw = A * s, ph = s;
    const ox = boxW / 2 - ((x0 + x1) / 2) * pw;
    const oy = boxH / 2 - ((y0 + y1) / 2) * ph;
    return { s, pw, ph, ox, oy };
  }

  // Off times offered in the Schedule popover ("HH:MM", in night order).
  // Guests: strictly after tonight's on time (or the fallback without a sunset)
  // and no later than GUEST_LAST. Admins: any quarter hour from 17:00 to 03:00.
  const GUEST_LAST = '22:00', NIGHT_ENDS = '06:00';
  const nightMin = (t) => { const [hh, mm] = t.split(':').map(Number); return hh * 60 + mm + (t < NIGHT_ENDS ? 1440 : 0); };
  const guestAfter = (sc, fallback) => sc.on_at || fallback || '17:00';
  function guestOk(sc, t, fallback) {
    return nightMin(t) > nightMin(guestAfter(sc, fallback)) && nightMin(t) <= nightMin(GUEST_LAST);
  }
  function offTimeOptions(sc, o) {
    o = o || {};
    const admin = !!o.admin, out = [], last = admin ? 27 * 60 : 22 * 60;
    for (let m = 17 * 60; m <= last; m += 15) {
      const t = String(Math.floor(m / 60) % 24).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0');
      if (admin || guestOk(sc, t, o.guestFallback)) out.push(t);
    }
    // An admin's later off time stays selectable for guests, as long as it's after the on time.
    if (sc.off_time && !out.includes(sc.off_time)
        && (admin || nightMin(sc.off_time) > nightMin(guestAfter(sc, o.guestFallback)))) out.push(sc.off_time);
    const key = (t) => (t < NIGHT_ENDS ? '1' : '0') + t;
    return out.sort((x, y) => (key(x) < key(y) ? -1 : 1));
  }

  const api = { fitBox, offTimeOptions, nightMin, guestAfter, guestOk, GUEST_LAST, NIGHT_ENDS };
  root.GemstoneCore = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
