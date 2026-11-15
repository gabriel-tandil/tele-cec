-- Un texto chico en la esquina de abajo a la derecha: en Fotos, la fecha de cada foto (1 en el control) o el año
-- en «Un día como hoy».
-- tele-cec lo pide por el canal de control con  script-message-to tele_cec_corner show <grande> [<chico>]
-- (y clear para sacarlo). La capa la guarda este script: una capa pedida por el canal de control se borra sola
-- apenas se cierra esa conexión.
local ov = mp.create_osd_overlay("ass-events")
ov.res_x, ov.res_y = 1920, 1080

local function esc(s)
    return (tostring(s or ""):gsub("[{}\\]", ""))
end

mp.register_script_message("show", function(big, small)
    local s = "{\\an3\\pos(1880,1050)\\fs40\\b1\\bord2.5\\shad1\\1c&HFFFFFF&\\3c&H000000&\\1a&H20&}" .. esc(big)
    if small and small ~= "" then
        s = s .. "{\\fs26\\b0}  ·  " .. esc(small)
    end
    ov.data = s
    ov:update()
end)

mp.register_script_message("clear", function()
    ov:remove()
end)
