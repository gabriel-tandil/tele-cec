-- tele_cec_vo.lua: elige con qué salida dibuja mpv cada archivo (Raspberry Pi).
--
-- En la Pi, Vulkan (la salida «gpu» de mpv) no sabe leer las imágenes que entrega el decodificador de video por
-- hardware: el video se veía todo azul (o violeta con gpu-next). Y la salida que sí las muestra bien y fluido
-- (dmabuf-wayland: el cuadro decodificado va directo a la pantalla) no muestra fotos. Entonces, antes de abrir
-- cada archivo:
--   - un video que la Pi decodifica por hardware (H.264, HEVC): dmabuf-wayland, con el decodificador de hardware;
--   - una foto, la tapa de un disco o un video en otro formato: gpu (Vulkan), sin el decodificador de hardware.
-- Cambiar de salida en el mismo mpv anda (la ventana sigue en pantalla completa); solo se cambia cuando hace falta.

local mp = require "mp"
local msg = require "mp.msg"

local opts = {
    video_vo = "dmabuf-wayland",
    video_hwdec = "v4l2m2m,drm",   -- v4l2m2m: H.264 (bcm2835-codec); drm: HEVC (rpi-hevc-dec)
    image_vo = "gpu",
    hw_codecs = "h264,hevc",
}
require("mp.options").read_options(opts, "tele_cec_vo")

local hw = {}
for c in opts.hw_codecs:gmatch("[^,%s]+") do hw[c] = true end

local current_vo = mp.get_property("options/vo", "")

-- "video" (va por hardware), "image" (foto, tapa o video que se decodifica con el procesador) o nil (solo sonido)
local function kind(tracks)
    local picture = false
    for _, t in ipairs(tracks or {}) do
        if t.type == "video" then
            if not t.image and not t.albumart then
                return hw[t.codec or ""] and "video" or "image"
            end
            picture = true
        end
    end
    return picture and "image" or nil
end

mp.add_hook("on_preloaded", 50, function()
    local k = kind(mp.get_property_native("track-list"))
    if not k then return end   -- sin imagen: que quede la salida que estaba (no hace falta recrear la ventana)
    local vo = (k == "video") and opts.video_vo or opts.image_vo
    local hwdec = (k == "video") and opts.video_hwdec or "no"
    if mp.get_property("options/hwdec") ~= hwdec then
        mp.set_property("options/hwdec", hwdec)
    end
    if current_vo ~= vo then
        msg.info(string.format("salida de video: %s (%s)", vo, k))
        mp.set_property("options/vo", vo)
        current_vo = vo
    end
end)
