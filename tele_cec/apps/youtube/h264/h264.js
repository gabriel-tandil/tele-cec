// La carga tele-cec en el Chromium de YouTube (apps.youtube.force_h264): YouTube pregunta qué formatos se pueden
// reproducir y elige el mejor; si VP9 y AV1 «no se pueden», usa H.264, que la Raspberry Pi decodifica por hardware.
(() => {
  const blocked = (type) => /vp0?9|av01|av1/i.test(String(type || ""));
  for (const MS of [window.MediaSource, window.ManagedMediaSource, window.WebKitMediaSource]) {
    if (MS && typeof MS.isTypeSupported === "function") {
      const original = MS.isTypeSupported.bind(MS);
      MS.isTypeSupported = (type) => (blocked(type) ? false : original(type));
    }
  }
  const canPlay = HTMLMediaElement.prototype.canPlayType;
  HTMLMediaElement.prototype.canPlayType = function (type) {
    return blocked(type) ? "" : canPlay.call(this, type);
  };
})();
