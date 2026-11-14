-- tele_cec_osd.lua: la barra de mpv para la TV (tele-cec).
--
-- Se maneja con el control remoto por mensajes (script-message-to tele_cec_osd ...), no con el mouse:
--   show [segundos]   mostrar la barra un rato
--   scrub <±segundos> buscar: mueve un cursor sobre la barra (con la miniatura de ese punto, si está thumbfast)
--                     y salta ahí cuando se deja de tocar (o con «ok»)
--   ok                si se está buscando, salta ahí; si no, play / pausa
--   cancel            deja de buscar sin saltar
-- La barra aparece sola al pausar, al saltar y al empezar cada archivo; en pausa queda a la vista. Con audio=yes
-- (Música) queda siempre a la vista, con el tema, el artista y el disco.

local mp = require "mp"
local utils = require "mp.utils"

local opts = {
    audio = false,        -- Música: siempre a la vista
    title = true,         -- mostrar el título del archivo
    show_s = 4,           -- cuánto queda a la vista después de un cambio
    scrub_delay = 1.3,    -- segundos sin tocar ◀ ▶ antes de saltar al punto elegido
}
require("mp.options").read_options(opts, "tele_cec_osd")

local W, H = 1920, 1080
local ov = mp.create_osd_overlay("ass-events")
ov.res_x, ov.res_y = W, H
local visible_until = 0
local scrub_target = nil
local scrub_timer = nil
local thumb_shown = false
local thumbfast = { width = 0, height = 0, disabled = true, available = false }

mp.register_script_message("thumbfast-info", function(json)
    local data = utils.parse_json(json)
    if type(data) == "table" then thumbfast = data end
end)

local function fmt(t)
    t = math.max(0, math.floor(t or 0))
    local h, m, s = math.floor(t / 3600), math.floor(t / 60) % 60, t % 60
    if h > 0 then return string.format("%d:%02d:%02d", h, m, s) end
    return string.format("%d:%02d", m, s)
end

local function esc(s)
    s = tostring(s or "")
    s = s:gsub("\\", "\\\226\129\160"):gsub("{", "\\{"):gsub("}", "\\}"):gsub("\n", " ")
    return s
end

local function meta(key)
    local m = mp.get_property_native("metadata") or {}
    for k, v in pairs(m) do
        if k:lower() == key then return v end
    end
    return nil
end

local function has_picture()
    local v = mp.get_property_native("current-tracks/video")
    return type(v) == "table"
end

local function is_video()
    local v = mp.get_property_native("current-tracks/video")
    return type(v) == "table" and not v.image and not v.albumart
end

local function clear_thumb()
    if thumb_shown then
        mp.commandv("script-message-to", "thumbfast", "clear")
        thumb_shown = false
    end
end

local function box(x1, y1, x2, y2, color, alpha)
    return string.format("{\\an7\\pos(0,0)\\bord0\\shad0\\1c&H%s&\\1a&H%s&\\p1}m %d %d l %d %d l %d %d l %d %d{\\p0}\n",
        color, alpha, x1, y1, x2, y1, x2, y2, x1, y2)
end

local function text(x, y, an, size, s, color, bold)
    return string.format("{\\an%d\\pos(%d,%d)\\fs%d\\bord2\\shad0\\3c&H000000&\\1c&H%s&\\b%d\\fnSans}%s\n",
        an, x, y, size, color or "FFFFFF", bold and 1 or 0, s)
end

local function render()
    local paused = mp.get_property_native("pause")
    local now = mp.get_time()
    local show = opts.audio or paused or scrub_target ~= nil or now < visible_until
    if not show or mp.get_property_native("idle-active") then
        if ov.data ~= "" then ov.data = ""; ov:update() end
        clear_thumb()
        return
    end
    local dur = mp.get_property_number("duration", 0) or 0
    local pos = mp.get_property_number("time-pos", 0) or 0
    local t = scrub_target or pos
    local out = {}
    local audio_screen = opts.audio and not has_picture()

    -- Música sin tapa: el tema bien grande en el medio
    if audio_screen then
        local title = meta("title") or mp.get_property("media-title", "")
        out[#out + 1] = text(W / 2, H / 2 - 70, 5, 84, esc(title), "FFFFFF", true)
        local artist = meta("artist") or meta("album_artist")
        if artist then out[#out + 1] = text(W / 2, H / 2 + 30, 5, 52, esc(artist), "DDDDDD") end
        local album = meta("album")
        if album then out[#out + 1] = text(W / 2, H / 2 + 100, 5, 40, esc(album), "AAAAAA") end
    end

    -- la franja de abajo
    local top = 850
    out[#out + 1] = box(0, top, W, H, "000000", "50")
    local x1, x2, by = 120, W - 120, 985
    if opts.title and not audio_screen then
        local title = meta("title") or mp.get_property("media-title", "")
        local line = esc(title)
        if opts.audio then
            local artist = meta("artist") or meta("album_artist")
            if artist then line = line .. "{\\c&HBBBBBB&}  ·  " .. esc(artist) end
        end
        out[#out + 1] = text(x1, top + 45, 4, 46, line, "FFFFFF", true)
    end
    local count = mp.get_property_number("playlist-count", 0) or 0
    if count > 1 then
        local n = (mp.get_property_number("playlist-pos", 0) or 0) + 1
        out[#out + 1] = text(x2, top + 45, 6, 34, string.format("%d de %d", n, count), "BBBBBB")
    end
    -- la barra
    local frac = (dur > 0) and math.min(1, math.max(0, t / dur)) or 0
    local played = (dur > 0) and math.min(1, math.max(0, pos / dur)) or 0
    out[#out + 1] = box(x1, by - 6, x2, by + 6, "FFFFFF", "C0")
    out[#out + 1] = box(x1, by - 6, x1 + math.floor((x2 - x1) * played), by + 6, "FFFFFF", "10")
    local cx = x1 + math.floor((x2 - x1) * frac)
    if scrub_target then
        out[#out + 1] = box(cx - 4, by - 26, cx + 4, by + 26, "66D1FF", "00")
        local delta = scrub_target - pos
        local sign = delta >= 0 and "+" or "−"
        out[#out + 1] = text(cx, by - 40, 2, 44, string.format("%s  (%s%s)", fmt(scrub_target), sign, fmt(math.abs(delta))),
            "66D1FF", true)
    end
    if paused and not scrub_target then  -- debajo de la barra, entre los tiempos (arriba lo pisaba un título largo)
        out[#out + 1] = text(W / 2, by + 50, 5, 36, "❚❚ En pausa", "66D1FF", true)
    end
    out[#out + 1] = text(x1, by + 50, 4, 36, fmt(t), "FFFFFF")
    if dur > 0 then out[#out + 1] = text(x2, by + 50, 6, 36, fmt(dur), "BBBBBB") end
    if opts.audio then  -- Música: aleatorio y repetir (amarillo y verde), a la izquierda de la duración
        local modes = {}
        if mp.get_property_bool("shuffle", false) then modes[#modes + 1] = "aleatorio" end
        local lf, lp = mp.get_property("loop-file", "no"), mp.get_property("loop-playlist", "no")
        if lf ~= "no" then modes[#modes + 1] = "repite este tema"
        elseif lp ~= "no" then modes[#modes + 1] = "repite la lista" end
        if #modes > 0 then
            out[#out + 1] = text(x2 - 150, by + 50, 6, 32, table.concat(modes, "  ·  "), "66D1FF", true)
        end
    end

    -- la miniatura del punto elegido (videos, con thumbfast)
    if scrub_target and is_video() and thumbfast.available and not thumbfast.disabled
        and (thumbfast.width or 0) > 0 then
        local ow, oh = mp.get_osd_size()
        if ow and ow > 0 then
            local sx, sy = ow / W, oh / H
            local tw, th = thumbfast.width, thumbfast.height
            local px = math.floor(math.min(ow - tw - 10, math.max(10, cx * sx - tw / 2)))
            local py = math.floor((by - 100) * sy - th)
            -- un marco detrás (en coordenadas de la barra)
            out[#out + 1] = box(math.floor((px - 6) / sx), math.floor((py - 6) / sy),
                math.floor((px + tw + 6) / sx), math.floor((py + th + 6) / sy), "000000", "20")
            mp.commandv("script-message-to", "thumbfast", "thumb", tostring(scrub_target), tostring(px), tostring(py))
            thumb_shown = true
        end
    else
        clear_thumb()
    end
    ov.data = table.concat(out)
    ov:update()
end

local function show(secs)
    visible_until = mp.get_time() + (tonumber(secs) or opts.show_s)
    render()
end

local function commit()
    if scrub_timer then scrub_timer:kill(); scrub_timer = nil end
    if scrub_target then
        local target = scrub_target
        scrub_target = nil
        clear_thumb()
        mp.commandv("seek", tostring(target), "absolute+keyframes")
        show()
    end
end

mp.register_script_message("show", show)
mp.register_script_message("scrub", function(delta)
    local d = tonumber(delta) or 0
    local dur = mp.get_property_number("duration", 0) or 0
    local base = scrub_target or mp.get_property_number("time-pos", 0) or 0
    local target = base + d
    if dur > 0 then target = math.min(target, math.max(0, dur - 2)) end
    scrub_target = math.max(0, target)
    if scrub_timer then scrub_timer:kill() end
    scrub_timer = mp.add_timeout(opts.scrub_delay, commit)
    render()
end)
mp.register_script_message("ok", function()
    if scrub_target then
        commit()
    else
        mp.commandv("cycle", "pause")
        show()
    end
end)
mp.register_script_message("cancel", function()
    if scrub_timer then scrub_timer:kill(); scrub_timer = nil end
    scrub_target = nil
    clear_thumb()
    show(1.5)
end)

mp.observe_property("pause", "bool", function() show() end)
for _, prop in ipairs({"shuffle", "loop-file", "loop-playlist"}) do  -- también si lo cambia el celular
    mp.observe_property(prop, "native", function(_, _) if opts.audio then show() end end)
end
mp.register_event("file-loaded", function()
    scrub_target = nil
    show(opts.show_s + 1)
end)
mp.register_event("seek", function()
    if not scrub_target then visible_until = math.max(visible_until, mp.get_time() + 2) end
end)
mp.observe_property("osd-dimensions", "native", function() render() end)
mp.add_periodic_timer(0.25, render)
