-- tele_cec_vo.lua: elige con qué salida dibuja mpv cada archivo (Raspberry Pi).
--
-- La salida que muestra bien y fluido los videos decodificados por hardware (dmabuf-wayland: el cuadro decodificado
-- va directo a la pantalla) no muestra fotos; y la que muestra fotos (gpu, con OpenGL: tele-cec la arranca con
-- --gpu-api=opengl) no puede con esos cuadros sin trabarse (y con Vulkan, ni los lee: se veía todo azul).
-- Entonces, antes de abrir cada archivo:
--   - un video que la Pi decodifica por hardware (H.264, HEVC): dmabuf-wayland, con el decodificador de hardware;
--   - una foto, la tapa de un disco o un video en otro formato: gpu, sin el decodificador de hardware.
-- Cambiar de salida en el mismo mpv anda (la ventana sigue en pantalla completa); solo se cambia cuando hace falta.
--
-- Además, la placa de video de la Pi no acepta imágenes de más de 4096 píxeles de lado: una foto de 4160 × 3120
-- (13 MP) salía negra (o azul). Esas se achican antes de dibujarlas (a 4096 del lado mayor).
--
-- Saltos de brillo con subtítulos: con dmabuf-wayland, mientras no hay nada encima del video, labwc le pasa el cuadro
-- directo a la placa de video (que lo convierte a colores con la fórmula de BT.601), y cuando aparece un subtítulo
-- o la barra lo compone con la GPU (que lo convierte distinto: medido, en una escena oscura, ~1,5 niveles más claro).
-- Cada subtítulo cambiaba de un camino al otro, y en películas oscuras se veía como un salto de brillo. mpv 0.40 no
-- le dice a labwc qué colores tiene el video (wp_color_representation), así que no hay forma de que coincidan: en
-- cambio, mientras se ve un video se deja un píxel casi transparente en el OSD y labwc compone siempre (keep_composed).
-- Medido en una Pi 400: sin cuadros perdidos de más (también con 1080p60).
--
-- Zoom en pausa: dmabuf-wayland aplica el zoom, el corrimiento y el giro recién cuando dibuja el cuadro siguiente,
-- y con el video en pausa no llega ninguno (en Fotos, el primer ▲ pausa: el zoom no se veía hasta seguir). Un cambio
-- en el OSD sí hace que dibuje: cuando cambian, el píxel se corre de lugar (redraw).

local mp = require "mp"
local msg = require "mp.msg"

local opts = {
    video_vo = "dmabuf-wayland",
    video_hwdec = "v4l2m2m,drm",   -- v4l2m2m: H.264 (bcm2835-codec); drm: HEVC (rpi-hevc-dec)
    image_vo = "gpu",
    hw_codecs = "h264,hevc",
    max_texture = 4096,            -- lado máximo de una imagen para la placa de video (0 = no achicar)
    keep_composed = true,          -- videos por hardware: siempre compuestos por labwc (sin saltos de brillo)
}
require("mp.options").read_options(opts, "tele_cec_vo")

local hw = {}
for c in opts.hw_codecs:gmatch("[^,%s]+") do hw[c] = true end

local current_vo = mp.get_property("options/vo", "")

local scaled = false   -- si el filtro para achicar lo puso este script (para sacarlo después)

-- un píxel en la esquina con alfa FE (casi transparente del todo): obliga a labwc a componer el video
local veil = mp.create_osd_overlay("ass-events")
veil.res_x, veil.res_y, veil.z = 1920, 1080, -1000
local function veil_at(x)
    return "{\\an7\\pos(" .. x .. ",0)\\bord0\\shad0\\1c&H000000&\\1a&HFE&\\p1}m 0 0 l 1 0 1 1 0 1{\\p0}"
end
veil.data = veil_at(0)
local veil_on = false
local veil_x = 0

local function set_veil(on)
    if on == veil_on then return end
    if on then veil:update() else veil:remove() end
    veil_on = on
end

-- Que dmabuf-wayland vuelva a dibujar (aunque esté en pausa): el píxel cambia de lugar (0 ↔ 1).
local function redraw()
    if current_vo ~= opts.video_vo then return end
    veil_x = 1 - veil_x
    veil.data = veil_at(veil_x)
    veil:update()
    if not veil_on then veil:remove() end
end
for _, prop in ipairs({"video-zoom", "video-pan-x", "video-pan-y", "video-rotate"}) do
    mp.observe_property(prop, "native", redraw)
end

-- "video" (va por hardware), "image" (foto, tapa o video que se decodifica con el procesador) o nil (solo sonido);
-- y la pista de imagen (para ver su tamaño)
local function kind(tracks)
    local picture = nil
    for _, t in ipairs(tracks or {}) do
        if t.type == "video" then
            if not t.image and not t.albumart then
                return (hw[t.codec or ""] and "video" or "image"), t
            end
            picture = picture or t
        end
    end
    return picture and "image" or nil, picture
end

local function fit_texture(k, track)
    local w, h = tonumber(track and track["demux-w"]) or 0, tonumber(track and track["demux-h"]) or 0
    local big = k == "image" and opts.max_texture > 0 and math.max(w, h) > opts.max_texture
    if big then
        local m = opts.max_texture
        msg.info(string.format("imagen de %dx%d: la achico a %d de lado (la placa de video no acepta más)", w, h, m))
        mp.set_property("vf", string.format("lavfi=[scale=w=%d:h=%d:force_original_aspect_ratio=decrease]", m, m))
        scaled = true
    elseif scaled then
        mp.set_property("vf", "")
        scaled = false
    end
end

mp.add_hook("on_preloaded", 50, function()
    local k, track = kind(mp.get_property_native("track-list"))
    fit_texture(k, track)
    if not k then return end   -- sin imagen: que quede la salida que estaba (no hace falta recrear la ventana)
    local vo = (k == "video") and opts.video_vo or opts.image_vo
    local hwdec = (k == "video") and opts.video_hwdec or "no"
    if mp.get_property("options/hwdec") ~= hwdec then
        mp.set_property("options/hwdec", hwdec)
    end
    set_veil(opts.keep_composed and vo == opts.video_vo)
    if current_vo ~= vo then
        msg.info(string.format("salida de video: %s (%s)", vo, k))
        mp.set_property("options/vo", vo)
        current_vo = vo
    end
end)
