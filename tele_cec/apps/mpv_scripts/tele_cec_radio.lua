-- tele_cec_radio.lua: la pantalla de la Radio en la TV (tele-cec).
--
-- Arriba, la emisora y «En vivo»; en el medio, el tema que está sonando (lo manda la emisora: «Artista - Tema»),
-- con la tapa del disco a la izquierda si tele-cec la encontró en línea; abajo, cuál de las radios es y qué teclas
-- cambian. Mientras se conecta o se corta la señal: «Conectando…».
-- El nombre de cada emisora viene de la lista (#EXTINF de la lista .m3u que arma tele-cec).
-- La tapa la manda tele-cec por el canal de control:  script-message-to tele_cec_radio cover <bgra> <w> <h>
-- (píxeles BGRA crudos, de covers.py) y  nocover  la saca.

local mp = require "mp"

local W, H = 1920, 1080
local COVER_X = 300       -- dónde va la tapa (en 1080p); el texto, a su derecha
local COVER_GAP = 80
local ov = mp.create_osd_overlay("ass-events")
ov.res_x, ov.res_y = W, H
local cover = nil         -- {w=, h=} mientras hay una tapa dibujada (overlay 1)

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

local function text(x, y, an, size, s, color, bold)
    return string.format("{\\an%d\\pos(%d,%d)\\fs%d\\bord2\\shad0\\3c&H000000&\\1c&H%s&\\b%d\\fnSans}%s\n",
        an, x, y, size, color or "FFFFFF", bold and 1 or 0, s)
end

local function station()
    local pos = mp.get_property_number("playlist-pos", -1)
    if pos < 0 then return "Radio" end
    local name = mp.get_property("playlist/" .. pos .. "/title", "")
    if name == "" then name = meta("icy-name") or "Radio" end
    return name
end

-- «Artista - Tema» → tema, artista (si no tiene el guion, todo es el título)
local function song()
    local t = meta("icy-title") or ""
    t = t:gsub("^%s+", ""):gsub("%s+$", "")
    if t == "" then return nil, nil end
    local artist, title = t:match("^(.-)%s+%-%s+(.+)$")
    if artist and artist ~= "" then return title, artist end
    return t, nil
end

local function utf8_len(s)
    local _, n = s:gsub("[^\128-\191]", "")
    return n
end

local function render(force)
    local out = {}
    local idle = mp.get_property_native("idle-active")
    local waiting = idle or mp.get_property_native("paused-for-cache") or mp.get_property_native("seeking")
        or mp.get_property("audio-codec-name", "") == ""
    local paused = mp.get_property_native("pause")

    -- arriba: la emisora y «En vivo»
    out[#out + 1] = text(120, 110, 4, 56, esc(station()), "FFFFFF", true)
    if paused then
        out[#out + 1] = text(W - 120, 110, 6, 40, "❚❚ En pausa", "66D1FF", true)
    elseif waiting then
        out[#out + 1] = text(W - 120, 110, 6, 40, "Conectando…", "BBBBBB", true)
    else
        out[#out + 1] = text(W - 120, 110, 6, 40, "{\\1c&H4444FF&}●{\\1c&HFFFFFF&} En vivo", "FFFFFF", true)
    end

    -- en el medio: lo que suena (con la tapa a la izquierda, el texto va a su derecha)
    local title, artist = song()
    if title then
        if cover then
            local x = COVER_X + cover.w + COVER_GAP
            local big = utf8_len(title) > 22 and 60 or 76   -- un título largo, un poco más chico (no se corta)
            out[#out + 1] = text(x, H / 2 - 50, 4, big, esc(title), "FFFFFF", true)
            if artist then out[#out + 1] = text(x, H / 2 + 50, 4, 50, esc(artist), "DDDDDD") end
        else
            out[#out + 1] = text(W / 2, H / 2 - 60, 5, 84, esc(title), "FFFFFF", true)
            if artist then out[#out + 1] = text(W / 2, H / 2 + 50, 5, 56, esc(artist), "DDDDDD") end
        end
    else
        local about = meta("icy-description") or ""
        if #about > 160 then about = about:sub(1, 157):gsub("[\128-\191]*$", ""):gsub("[\192-\255]$", "") .. "…" end
        out[#out + 1] = text(W / 2, H / 2 - 40, 5, 84, esc(station()), "FFFFFF", true)
        if about ~= "" then out[#out + 1] = text(W / 2, H / 2 + 60, 8, 36, esc(about), "AAAAAA") end
    end

    -- abajo: cuál es y cómo cambiar
    local count = mp.get_property_number("playlist-count", 0) or 0
    local hint = "OK pausa  ·  ▲▼ volumen"
    if count > 1 then
        local n = (mp.get_property_number("playlist-pos", 0) or 0) + 1
        out[#out + 1] = text(W - 120, H - 80, 6, 34, string.format("Radio %d de %d", n, count), "BBBBBB")
        hint = "⏮ ⏭ otra radio  ·  " .. hint
    end
    out[#out + 1] = text(120, H - 80, 4, 34, hint, "888888")

    local data = table.concat(out)
    if force or data ~= ov.data then
        ov.data = data
        ov:update()
    end
end

-- La tapa: un overlay de mpv con los píxeles crudos. Las medidas son en 1080p; si la pantalla es otra, se escala.
local function place_cover(file, w, h)
    local dims = mp.get_property_native("osd-dimensions") or {}
    local ow, oh = dims.w or 0, dims.h or 0
    if ow < 1 or oh < 1 then ow, oh = W, H end   -- todavía sin ventana: como si fuera 1080p
    local scale = math.min(ow / W, oh / H)
    local dw, dh = math.floor(w * scale), math.floor(h * scale)
    local x = math.floor(COVER_X * scale)
    local y = math.floor((oh - dh) / 2)
    mp.commandv("overlay-add", "1", tostring(x), tostring(y), file, "0", "bgra", tostring(w), tostring(h),
        tostring(w * 4), tostring(dw), tostring(dh))
end

mp.register_script_message("cover", function(file, w, h)
    w, h = tonumber(w), tonumber(h)
    if not file or not w or not h then return end
    cover = {file = file, w = w, h = h}
    place_cover(file, w, h)
    render(true)
end)

mp.register_script_message("nocover", function()
    if cover then mp.commandv("overlay-remove", "1") end
    cover = nil
    render(true)
end)

for _, prop in ipairs({"metadata", "pause", "paused-for-cache", "idle-active", "playlist-pos", "audio-codec-name"}) do
    mp.observe_property(prop, "native", function() render() end)
end
mp.observe_property("osd-dimensions", "native", function()
    if cover then place_cover(cover.file, cover.w, cover.h) end
    render()
end)
mp.add_periodic_timer(1, render)
