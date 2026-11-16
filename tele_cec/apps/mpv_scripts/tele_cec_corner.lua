-- Un texto chico en la esquina de abajo a la derecha: en Fotos, la fecha de cada foto (CC en el control) o el año
-- en «Un día como hoy», y un globo terráqueo a la derecha si la foto o el video tiene la ubicación (GPS).
-- tele-cec lo pide por el canal de control con  script-message-to tele_cec_corner show <grande> [<chico>] [globe]
-- (y clear para sacarlo). La capa la guarda este script: una capa pedida por el canal de control se borra sola
-- apenas se cierra esa conexión.
local ov = mp.create_osd_overlay("ass-events")
ov.res_x, ov.res_y = 1920, 1080

local RIGHT, BOTTOM = 1880, 1050   -- la esquina del texto
local R = 17                       -- radio del globo
local GAP = 16                     -- entre el texto y el globo

local function esc(s)
    return (tostring(s or ""):gsub("[{}\\]", ""))
end

local function n(v)
    return string.format("%.1f", v)
end

-- Una elipse de radios rx, ry centrada en (cx, cy), con cuatro curvas de Bézier (dibujo de ASS).
local function ellipse(cx, cy, rx, ry)
    local k = 0.5523
    return table.concat({
        "m", n(cx), n(cy - ry),
        "b", n(cx + k * rx), n(cy - ry), n(cx + rx), n(cy - k * ry), n(cx + rx), n(cy),
        "b", n(cx + rx), n(cy + k * ry), n(cx + k * rx), n(cy + ry), n(cx), n(cy + ry),
        "b", n(cx - k * rx), n(cy + ry), n(cx - rx), n(cy + k * ry), n(cx - rx), n(cy),
        "b", n(cx - rx), n(cy - k * ry), n(cx - k * rx), n(cy - ry), n(cx), n(cy - ry),
    }, " ")
end

-- El globo: el contorno, un meridiano (elipse angosta), el eje, el ecuador y dos paralelos. Son trazos sin
-- relleno: lo que se ve es el borde (\bord) de cada uno. Cada trazo va en su propio evento (libass une las figuras
-- de un mismo dibujo y se perderían las de adentro), dibujado desde (0, 0) y puesto en su lugar con \pos.
local function globe_parts()
    local c, w, mr = R, 0.866 * R, 0.42 * R
    return {
        {0, 0, ellipse(R, R, R, R)},
        {c - mr, 0, ellipse(mr, R, mr, R)},
        {c, 0, "m 0 0 l 0 " .. n(2 * R)},
        {0, c, "m 0 0 l " .. n(2 * R) .. " 0"},
        {c - w, 0.5 * R, "m 0 0 l " .. n(2 * w) .. " 0"},
        {c - w, 1.5 * R, "m 0 0 l " .. n(2 * w) .. " 0"},
    }
end
local GLOBE = globe_parts()

local function globe_events()
    -- Primero un borde oscuro más grueso (para que se vea sobre una foto clara); encima, el trazo blanco.
    local x0, y0 = RIGHT - 2 * R, BOTTOM - 2 * R - 3
    local out = {}
    for _, style in ipairs({"\\bord3.2\\3c&H000000&\\3a&H60&", "\\bord1.6\\3c&HFFFFFF&\\3a&H10&"}) do
        for _, part in ipairs(GLOBE) do
            out[#out + 1] = "{\\an7\\pos(" .. n(x0 + part[1]) .. "," .. n(y0 + part[2]) .. ")\\shad0\\1a&HFF&"
                .. style .. "\\p1}" .. part[3] .. "{\\p0}"
        end
    end
    return table.concat(out, "\n")
end

mp.register_script_message("show", function(big, small, globe)
    local with_globe = globe == "globe"
    local right = with_globe and (RIGHT - 2 * R - GAP) or RIGHT
    local s = ""
    if (big and big ~= "") or (small and small ~= "") then
        s = "{\\an3\\pos(" .. right .. "," .. BOTTOM .. ")\\fs40\\b1\\bord2.5\\shad1\\1c&HFFFFFF&\\3c&H000000&\\1a&H20&}"
            .. esc(big)
        if small and small ~= "" then
            s = s .. "{\\fs26\\b0}  ·  " .. esc(small)
        end
    end
    if with_globe then
        s = (s ~= "" and (s .. "\n") or "") .. globe_events()
    end
    ov.data = s
    ov:update()
end)

mp.register_script_message("clear", function()
    ov:remove()
end)
