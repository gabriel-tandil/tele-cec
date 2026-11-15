-- tele_cec_radio.lua: la pantalla de la Radio en la TV (tele-cec).
--
-- Arriba, la emisora y «En vivo»; en el medio, el tema que está sonando (lo manda la emisora: «Artista - Tema»);
-- abajo, cuál de las radios es y qué teclas cambian. Mientras se conecta o se corta la señal: «Conectando…».
-- El nombre de cada emisora viene de la lista (#EXTINF de la lista .m3u que arma tele-cec).

local mp = require "mp"

local W, H = 1920, 1080
local ov = mp.create_osd_overlay("ass-events")
ov.res_x, ov.res_y = W, H

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

local function render()
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

    -- en el medio: lo que suena
    local title, artist = song()
    if title then
        out[#out + 1] = text(W / 2, H / 2 - 60, 5, 84, esc(title), "FFFFFF", true)
        if artist then out[#out + 1] = text(W / 2, H / 2 + 50, 5, 56, esc(artist), "DDDDDD") end
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
    if data ~= ov.data then
        ov.data = data
        ov:update()
    end
end

for _, prop in ipairs({"metadata", "pause", "paused-for-cache", "idle-active", "playlist-pos", "audio-codec-name"}) do
    mp.observe_property(prop, "native", function() render() end)
end
mp.observe_property("osd-dimensions", "native", function() render() end)
mp.add_periodic_timer(1, render)
