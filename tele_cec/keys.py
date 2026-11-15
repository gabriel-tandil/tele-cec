"""Tabla de códigos de tecla CEC (cec_user_control_code de libcec / cectypes.h).

Son los mismos códigos que usa cec-mini-kb en su map.txt (por ejemplo 0 = SELECT,
1 = UP, 13 = EXIT, 113 = F1_BLUE). En la configuración se puede usar el nombre
("UP"), el número decimal ("1") o hexadecimal ("0x01").
"""

from __future__ import annotations

CEC_KEYS: dict[int, str] = {
    0x00: "SELECT",
    0x01: "UP",
    0x02: "DOWN",
    0x03: "LEFT",
    0x04: "RIGHT",
    0x05: "RIGHT_UP",
    0x06: "RIGHT_DOWN",
    0x07: "LEFT_UP",
    0x08: "LEFT_DOWN",
    0x09: "ROOT_MENU",
    0x0A: "SETUP_MENU",
    0x0B: "CONTENTS_MENU",
    0x0C: "FAVORITE_MENU",
    0x0D: "EXIT",
    0x10: "TOP_MENU",
    0x11: "DVD_MENU",
    0x1D: "NUMBER_ENTRY_MODE",
    0x1E: "NUMBER11",
    0x1F: "NUMBER12",
    0x20: "NUMBER0",
    0x21: "NUMBER1",
    0x22: "NUMBER2",
    0x23: "NUMBER3",
    0x24: "NUMBER4",
    0x25: "NUMBER5",
    0x26: "NUMBER6",
    0x27: "NUMBER7",
    0x28: "NUMBER8",
    0x29: "NUMBER9",
    0x2A: "DOT",
    0x2B: "ENTER",
    0x2C: "CLEAR",
    0x2F: "NEXT_FAVORITE",
    0x30: "CHANNEL_UP",
    0x31: "CHANNEL_DOWN",
    0x32: "PREVIOUS_CHANNEL",
    0x33: "SOUND_SELECT",
    0x34: "INPUT_SELECT",
    0x35: "DISPLAY_INFORMATION",
    0x36: "HELP",
    0x37: "PAGE_UP",
    0x38: "PAGE_DOWN",
    0x40: "POWER",
    0x41: "VOLUME_UP",
    0x42: "VOLUME_DOWN",
    0x43: "MUTE",
    0x44: "PLAY",
    0x45: "STOP",
    0x46: "PAUSE",
    0x47: "RECORD",
    0x48: "REWIND",
    0x49: "FAST_FORWARD",
    0x4A: "EJECT",
    0x4B: "FORWARD",
    0x4C: "BACKWARD",
    0x4D: "STOP_RECORD",
    0x4E: "PAUSE_RECORD",
    0x50: "ANGLE",
    0x51: "SUB_PICTURE",
    0x52: "VIDEO_ON_DEMAND",
    0x53: "ELECTRONIC_PROGRAM_GUIDE",
    0x54: "TIMER_PROGRAMMING",
    0x55: "INITIAL_CONFIGURATION",
    0x56: "SELECT_BROADCAST_TYPE",
    0x57: "SELECT_SOUND_PRESENTATION",
    0x60: "PLAY_FUNCTION",
    0x61: "PAUSE_PLAY_FUNCTION",
    0x62: "RECORD_FUNCTION",
    0x63: "PAUSE_RECORD_FUNCTION",
    0x64: "STOP_FUNCTION",
    0x65: "MUTE_FUNCTION",
    0x66: "RESTORE_VOLUME_FUNCTION",
    0x67: "TUNE_FUNCTION",
    0x68: "SELECT_MEDIA_FUNCTION",
    0x69: "SELECT_AV_INPUT_FUNCTION",
    0x6A: "SELECT_AUDIO_INPUT_FUNCTION",
    0x6B: "POWER_TOGGLE_FUNCTION",
    0x6C: "POWER_OFF_FUNCTION",
    0x6D: "POWER_ON_FUNCTION",
    0x71: "F1_BLUE",
    0x72: "F2_RED",
    0x73: "F3_GREEN",
    0x74: "F4_YELLOW",
    0x75: "F5",
    0x76: "DATA",
    0x91: "AN_RETURN",
    0x96: "AN_CHANNELS_LIST",
}

NAME_TO_CODE: dict[str, int] = {name: code for code, name in CEC_KEYS.items()}

# Descripción en castellano de las teclas más comunes (solo para mostrar).
DESCRIPCION: dict[str, str] = {
    "SELECT": "OK",
    "UP": "flecha arriba",
    "DOWN": "flecha abajo",
    "LEFT": "flecha izquierda",
    "RIGHT": "flecha derecha",
    "EXIT": "salir / atrás",
    "AN_RETURN": "volver",
    "F1_BLUE": "azul",
    "F2_RED": "rojo",
    "F3_GREEN": "verde",
    "F4_YELLOW": "amarillo",
    "PLAY": "play",
    "PAUSE": "pausa",
    "STOP": "stop",
    "REWIND": "retroceso rápido",
    "FAST_FORWARD": "avance rápido",
    "FORWARD": "siguiente",
    "BACKWARD": "anterior",
    "CHANNEL_UP": "canal +",
    "CHANNEL_DOWN": "canal -",
    "VOLUME_UP": "volumen +",
    "VOLUME_DOWN": "volumen -",
    "MUTE": "silencio",
    "ROOT_MENU": "menú",
    "SETUP_MENU": "configuración",
    "CONTENTS_MENU": "contenidos",
    "DISPLAY_INFORMATION": "info",
    "DOT": "punto",
    "ELECTRONIC_PROGRAM_GUIDE": "guía",
    **{f"NUMBER{i}": f"número {i}" for i in range(10)},
}

# Alias cómodos para el modo de prueba por teclado (--teclado).
ALIAS: dict[str, str] = {
    "u": "UP", "arriba": "UP",
    "d": "DOWN", "abajo": "DOWN",
    "l": "LEFT", "izq": "LEFT", "izquierda": "LEFT",
    "r": "RIGHT", "der": "RIGHT", "derecha": "RIGHT",
    "ok": "SELECT", "enter": "SELECT",
    "b": "EXIT", "back": "EXIT", "atras": "EXIT", "atrás": "EXIT", "salir": "EXIT",
    "rojo": "F2_RED", "red": "F2_RED",
    "verde": "F3_GREEN", "green": "F3_GREEN",
    "amarillo": "F4_YELLOW", "yellow": "F4_YELLOW",
    "azul": "F1_BLUE", "blue": "F1_BLUE",
    "play": "PLAY", "pausa": "PAUSE", "pause": "PAUSE",
    "ff": "FAST_FORWARD", "rw": "REWIND",
    "m": "ROOT_MENU", "menu": "ROOT_MENU", "menú": "ROOT_MENU",
    **{str(i): f"NUMBER{i}" for i in range(10)},
}


def parse_key(text: str) -> int | None:
    """Convierte "UP", "1", "0x01", "CEC_USER_CONTROL_CODE_UP" o "CEC_CODE_UP" en un código."""
    s = str(text).strip()
    if not s:
        return None
    low = s.lower()
    if low in ALIAS:
        return NAME_TO_CODE[ALIAS[low]]
    try:
        n = int(s, 0)
    except ValueError:
        pass
    else:
        return n if 0 <= n <= 0xFF else None
    up = s.upper()
    for prefix in ("CEC_USER_CONTROL_CODE_", "CEC_CODE_"):
        if up.startswith(prefix):
            up = up[len(prefix):]
    return NAME_TO_CODE.get(up)


def key_name(code: int) -> str:
    return CEC_KEYS.get(code, f"DESCONOCIDA_0x{code:02X}")


def key_label(code: int) -> str:
    """Nombre + descripción, por ejemplo "F2_RED (rojo)"."""
    name = key_name(code)
    desc = DESCRIPCION.get(name)
    return f"{name} ({desc})" if desc else name


# Opcodes CEC que interesan para logs y para el modo aprendizaje.
OPCODES: dict[int, str] = {
    0x04: "IMAGE_VIEW_ON",
    0x0D: "TEXT_VIEW_ON",
    0x36: "STANDBY",
    0x44: "USER_CONTROL_PRESSED",
    0x45: "USER_CONTROL_RELEASE",
    0x46: "GIVE_OSD_NAME",
    0x47: "SET_OSD_NAME",
    0x82: "ACTIVE_SOURCE",
    0x83: "GIVE_PHYSICAL_ADDRESS",
    0x84: "REPORT_PHYSICAL_ADDRESS",
    0x85: "REQUEST_ACTIVE_SOURCE",
    0x86: "SET_STREAM_PATH",
    0x87: "DEVICE_VENDOR_ID",
    0x89: "VENDOR_COMMAND",
    0x8A: "VENDOR_REMOTE_BUTTON_DOWN",
    0x8B: "VENDOR_REMOTE_BUTTON_UP",
    0x8C: "GIVE_DEVICE_VENDOR_ID",
    0x8F: "GIVE_DEVICE_POWER_STATUS",
    0x90: "REPORT_POWER_STATUS",
    0x9D: "INACTIVE_SOURCE",
    0x9E: "CEC_VERSION",
    0x9F: "GET_CEC_VERSION",
    0xA0: "VENDOR_COMMAND_WITH_ID",
}
