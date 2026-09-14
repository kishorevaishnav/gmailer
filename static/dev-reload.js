"use strict";

(function () {
  let last = null;
  let wasDown = false;
  async function tick() {
    try {
      const res = await fetch("/api/dev-hash", { cache: "no-store" });
      if (!res.ok) throw new Error("bad hash");
      const data = await res.json();
      if (last === null) { last = data.hash; return; }
      if (wasDown || data.hash !== last) window.location.reload();
    } catch (e) {
      wasDown = true;
    }
  }
  setInterval(tick, 2500);
  tick();
})();
