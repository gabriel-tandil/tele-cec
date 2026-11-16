"""Panel en pantalla del menú de actividades (proceso aparte, GTK 3).

Lo lanza el servicio (ui/overlay.py) y queda abierto y oculto. Recibe órdenes en JSON por stdin, una por línea:

    {"op": "show", "title": "Actividades", "items": [{"name", "icon", "sub", "enabled", "current"}],
     "selected": 0, "hint": "…", "columns": 2}     (columns: en grilla, la 1.ª columna primero, sin desplazar)
    {"op": "select", "index": 2, "chip": false}   (chip: el foco en el botoncito de la fila, item["action"])
    {"op": "busy", "text": "Abriendo Videos…"}
    {"op": "toast", "text": "Sin videos", "ms": 2000}
    {"op": "hide"}
    {"op": "quit"}

Al arrancar escribe en stdout {"ready": true, "layer_shell": …}. Si stdin se cierra (el servicio terminó),
se cierra solo.

Teclado (TELE_CEC_MENU_KEYBOARD=1, lo pone el servicio según [menu] keyboard): mientras se ve una página que
se maneja con teclas (menú, ayuda, QR) el panel toma el teclado y escribe en stdout cada tecla con el nombre
de la tecla CEC equivalente, {"key": "UP"}, y {"release": true} al soltarla. Con un aviso, «Abriendo…» u
oculto, no toma el teclado: las teclas siguen yendo a la app de abajo.

En Wayland usa gtk-layer-shell (labwc y los compositores wlroots): la ventana va en la capa OVERLAY, por
encima de las apps en pantalla completa, centrada y sin tomar el foco del teclado salvo mientras el menú
está a la vista (y [menu] keyboard): la app de abajo sigue recibiendo sus teclas. Sin layer-shell usa una ventana común "siempre encima".

Para verlo sin el servicio:   python3 -m tele_cec.ui.menu_ui --demo
Para guardar una imagen:      python3 -m tele_cec.ui.menu_ui --captura menu.png
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango  # noqa: E402

try:
    gi.require_version("GtkLayerShell", "0.1")
    from gi.repository import GtkLayerShell  # noqa: E402
except (ValueError, ImportError):
    GtkLayerShell = None

# Íconos que vienen con tele-cec (baldosas SVG): "netflix.svg" en la configuración busca acá.
ICONS_DIR = Path(__file__).resolve().parent / "icons"

DEMO_ITEMS = [
    {"name": "Netflix", "icon": "netflix.svg", "sub": "", "enabled": True, "current": True},
    {"name": "Tidal", "icon": "tidal.svg", "sub": "", "enabled": True},
    {"name": "Videos", "icon": "videos.svg", "sub": "Pendrive KINGSTON", "enabled": True},
    {"name": "Fotos", "icon": "fotos.svg", "sub": "Sin fotos: conectá un pendrive", "enabled": False},
    {"name": "Juegos", "icon": "juegos.svg", "sub": "", "enabled": True},
]
DEFAULT_HINT = "▲ ▼ elegir · OK abrir · Atrás cerrar"
# Páginas que se manejan con teclas: con [menu] keyboard, mientras se ven, el panel toma el teclado.
INTERACTIVE_PAGES = {"menu", "help", "image"}
# Teclas del teclado → tecla CEC equivalente (el servicio las trata igual que las del control).
KEYBOARD_KEYS = {
    "Up": "UP", "KP_Up": "UP", "Down": "DOWN", "KP_Down": "DOWN",
    "Left": "LEFT", "KP_Left": "LEFT", "Right": "RIGHT", "KP_Right": "RIGHT",
    "Return": "SELECT", "KP_Enter": "SELECT", "ISO_Enter": "SELECT", "space": "SELECT",
    "Escape": "EXIT", "BackSpace": "EXIT",
    "Menu": "CONTENTS_MENU", "F1": "HELP",
    **{str(n): f"NUMBER{n}" for n in range(10)}, **{f"KP_{n}": f"NUMBER{n}" for n in range(10)},
}


def keyboard_key(keyval: int) -> str | None:
    """La tecla CEC que corresponde a una tecla del teclado (o None si no se usa)."""
    return KEYBOARD_KEYS.get(Gdk.keyval_name(keyval) or "")

MAX_ROWS = 7   # filas visibles a la vez: las listas largas (carpetas) se desplazan
MAX_HELP_ROWS = 10
# En la ayuda, los colores del control se muestran con un punto de su color.
KEY_COLORS = {"Rojo": "#ef4444", "Verde": "#22c55e", "Amarillo": "#eab308", "Azul": "#3b82f6"}


def _monitor_geometry() -> tuple[int, int]:
    display = Gdk.Display.get_default()
    mon = display.get_primary_monitor() if display else None
    if mon is None and display and display.get_n_monitors():
        mon = display.get_monitor(0)
    if mon is None:
        return 1920, 1080
    g = mon.get_geometry()
    return max(640, g.width), max(480, g.height)


def _css(font: int) -> bytes:
    def px(f: float) -> str:
        return f"{max(1, round(font * f))}px"
    return f"""
    window, .root {{ background-color: transparent; }}
    .panel {{
        background-color: rgba(18, 18, 22, 0.94);
        border: 2px solid rgba(255, 255, 255, 0.10);
        border-radius: {px(0.8)};
        padding: {px(0.9)} {px(1.0)};
        color: #f2f2f2;
        font-size: {px(1)};
    }}
    .title {{ font-size: {px(1.15)}; font-weight: bold; }}
    .row {{ border-radius: {px(0.45)}; padding: {px(0.35)} {px(0.6)}; }}
    .row.sel {{ background-color: #f2f2f2; color: #111111; }}
    .row.off {{ opacity: 0.45; }}
    .row.sel.off {{ opacity: 0.75; }}
    .num {{ font-size: {px(0.8)}; opacity: 0.6; }}
    .name {{ font-size: {px(1)}; font-weight: bold; }}
    .sub {{ font-size: {px(0.66)}; opacity: 0.72; }}
    .letter {{ font-size: {px(0.9)}; font-weight: bold; }}
    .badge {{
        font-size: {px(0.58)}; font-weight: bold;
        padding: {px(0.08)} {px(0.4)}; border-radius: {px(0.6)};
        background-color: rgba(255, 255, 255, 0.16);
    }}
    .row.sel .badge {{ background-color: rgba(0, 0, 0, 0.12); }}
    .chip {{
        font-size: {px(0.62)}; font-weight: bold;
        padding: {px(0.14)} {px(0.5)}; border-radius: {px(0.5)};
        border: 2px solid rgba(255, 255, 255, 0.28); color: rgba(255, 255, 255, 0.8);
    }}
    .row.sel .chip {{ border-color: rgba(0, 0, 0, 0.25); color: #333333; }}
    .row.near {{ background-color: rgba(255, 255, 255, 0.14); }}
    .row.near .chip {{ background-color: #f2f2f2; color: #111111; border-color: #f2f2f2; }}
    .row.near.off {{ opacity: 0.95; }}   /* sin contenido: el botoncito («Carpetas») justo sirve para elegir otra */
    .row.near.off .name, .row.near.off .sub {{ opacity: 0.5; }}
    .pct {{ font-size: {px(0.8)}; font-weight: bold; font-feature-settings: "tnum"; }}
    .row levelbar trough {{ min-height: {px(0.28)}; border: none; padding: 0; border-radius: {px(0.2)};
        background-image: none; background-color: rgba(255, 255, 255, 0.16); box-shadow: none; }}
    .row levelbar trough block {{ min-height: {px(0.28)}; border: none; border-radius: {px(0.2)};
        background-image: none; box-shadow: none; }}
    .row levelbar trough block.filled {{ background-color: #f2f2f2; }}
    .row levelbar trough block.empty {{ background-color: transparent; }}
    .row.sel levelbar trough {{ background-color: rgba(0, 0, 0, 0.14); }}
    .row.sel levelbar trough block.filled {{ background-color: #111111; }}
    .row.muted levelbar trough block.filled {{ background-color: rgba(255, 255, 255, 0.4); }}
    .row.sel.muted levelbar trough block.filled {{ background-color: rgba(0, 0, 0, 0.35); }}
    .row.muted .pct {{ opacity: 0.5; }}
    .hint {{ font-size: {px(0.6)}; opacity: 0.62; }}
    .more {{ font-size: {px(0.55)}; opacity: 0.55; }}
    .help-keys {{ font-size: {px(0.88)}; font-weight: bold; }}
    .help-desc {{ font-size: {px(0.88)}; }}
    .help-page {{ font-size: {px(0.62)}; opacity: 0.6; }}
    .msg {{ font-size: {px(0.7)}; font-weight: bold; color: #ffd166; }}
    .row.sel .msg {{ color: #8a5a00; }}
    .busy {{ font-size: {px(1)}; font-weight: bold; }}
    .qr {{ background-color: #ffffff; padding: {px(0.4)}; border-radius: {px(0.3)}; }}
    .qr-cell {{ padding: {px(0.2)} {px(0.6)}; opacity: 0.85; }}
    .qr-cell .qr {{ padding: {px(0.18)}; border-radius: {px(0.18)}; }}
    .qr-url {{ font-size: {px(0.6)}; font-weight: bold; color: #ffd166; }}
    .toast {{ font-size: {px(1.1)}; }}
    """.encode()


class Panel:
    def __init__(self, offscreen: bool = False):
        width, height = _monitor_geometry()
        self.font = max(18, round(height * 0.028))       # 30 px en 1080p
        self.width = max(round(self.font * 17), round(width * 0.34))
        self.icon_px = round(self.font * 1.7)
        self.rows: list[Gtk.Box] = []
        self.items: list[dict] = []
        self.selected = 0
        self.top = 0              # primera fila visible
        self.numbers = True       # números 1-9 a la izquierda (en el menú de actividades sí, en carpetas no)
        self.columns = 1          # el menú de actividades va en dos columnas
        self.chip = False         # el foco está en el botoncito de la fila elegida («Carpetas», «Conectar»)
        self.mode = "hidden"
        self.keyboard = os.environ.get("TELE_CEC_MENU_KEYBOARD", "") == "1" and not offscreen
        self._grabbing = False
        self._hint = DEFAULT_HINT
        self._toast_id = 0
        self._msg_id = 0

        provider = Gtk.CssProvider()
        provider.load_from_data(_css(self.font))
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)

        self.layer_shell = False
        if offscreen:
            self.win = Gtk.OffscreenWindow()
        else:
            self.win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
            self.win.set_title("tele-cec menú")
            self.win.set_decorated(False)
            self.win.set_resizable(False)
            self.win.set_accept_focus(False)
            self.win.set_focus_on_map(False)
            self.win.set_skip_taskbar_hint(True)
            self.win.set_skip_pager_hint(True)
            if GtkLayerShell is not None and _layer_shell_supported():
                GtkLayerShell.init_for_window(self.win)
                GtkLayerShell.set_layer(self.win, GtkLayerShell.Layer.OVERLAY)
                GtkLayerShell.set_namespace(self.win, "tele-cec")
                if hasattr(GtkLayerShell, "set_keyboard_mode"):
                    GtkLayerShell.set_keyboard_mode(self.win, GtkLayerShell.KeyboardMode.NONE)
                else:
                    GtkLayerShell.set_keyboard_interactivity(self.win, False)
                self.layer_shell = True  # sin anclas: el compositor la centra
            else:
                self.win.set_keep_above(True)
                self.win.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
                self.win.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
            screen = self.win.get_screen()
            visual = screen.get_rgba_visual()
            if visual is not None and screen.is_composited():
                self.win.set_visual(visual)
            self.win.set_app_paintable(True)
            self.win.connect("realize", self._pass_through_mouse)
            if self.keyboard:
                self.win.connect("key-press-event", self._on_key)
                self.win.connect("key-release-event", self._on_key_release)
        self.win.get_style_context().add_class("root")

        self.panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.panel.get_style_context().add_class("panel")
        self.win.add(self.panel)

        self.stack = Gtk.Stack()
        self.stack.set_hhomogeneous(False)
        self.stack.set_vhomogeneous(False)
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.panel.pack_start(self.stack, True, True, 0)

        # Página "menu"
        menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=round(self.font * 0.25))
        menu.set_size_request(self.width, -1)
        self.menu_page = menu
        self.title = Gtk.Label(label="Actividades", xalign=0)
        self.title.get_style_context().add_class("title")
        self.title.set_margin_bottom(round(self.font * 0.35))
        menu.pack_start(self.title, False, False, 0)
        self.more_up = Gtk.Label(label="▲ más arriba")
        self.more_up.get_style_context().add_class("more")
        menu.pack_start(self.more_up, False, False, 0)
        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=round(self.font * 0.15))
        menu.pack_start(self.list_box, False, False, 0)
        self.more_down = Gtk.Label(label="▼ más abajo")
        self.more_down.get_style_context().add_class("more")
        menu.pack_start(self.more_down, False, False, 0)
        # El QR para abrir la página del celular: en el hueco de abajo a la derecha de la grilla (_build_rows) o,
        # si no queda hueco, en una fila chica abajo de la lista.
        self._qr: dict | None = None
        self.qr_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.qr_row.set_margin_top(round(self.font * 0.3))
        menu.pack_start(self.qr_row, False, False, 0)
        self.hint = Gtk.Label(label=DEFAULT_HINT, xalign=0.5)
        self.hint.get_style_context().add_class("hint")
        self.hint.set_line_wrap(True)
        self.hint.set_margin_top(round(self.font * 0.45))
        menu.pack_start(self.hint, False, False, 0)
        self.stack.add_named(menu, "menu")

        # Página "help": tabla tecla → qué hace
        helpbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=round(self.font * 0.25))
        width, _h = _monitor_geometry()
        helpbox.set_size_request(min(round(width * 0.8), round(self.width * 1.6)), -1)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.help_title = Gtk.Label(label="Ayuda", xalign=0)
        self.help_title.get_style_context().add_class("title")
        head.pack_start(self.help_title, True, True, 0)
        self.help_page = Gtk.Label(label="", xalign=1)
        self.help_page.get_style_context().add_class("help-page")
        head.pack_end(self.help_page, False, False, 0)
        head.set_margin_bottom(round(self.font * 0.3))
        helpbox.pack_start(head, False, False, 0)
        self.help_up = Gtk.Label(label="▲ más arriba")
        self.help_up.get_style_context().add_class("more")
        helpbox.pack_start(self.help_up, False, False, 0)
        self.help_grid = Gtk.Grid(column_spacing=round(self.font * 1.0), row_spacing=round(self.font * 0.3))
        helpbox.pack_start(self.help_grid, False, False, 0)
        self.help_down = Gtk.Label(label="▼ más abajo")
        self.help_down.get_style_context().add_class("more")
        helpbox.pack_start(self.help_down, False, False, 0)
        self.help_hint = Gtk.Label(label="", xalign=0.5)
        self.help_hint.get_style_context().add_class("hint")
        self.help_hint.set_margin_top(round(self.font * 0.4))
        helpbox.pack_start(self.help_hint, False, False, 0)
        self.stack.add_named(helpbox, "help")

        # Página "image": título, una imagen (el QR para vincular un celular) y unas líneas al lado
        imgbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=round(self.font * 0.35))
        imgbox.set_size_request(min(round(width * 0.8), round(self.width * 1.6)), -1)
        self.img_title = Gtk.Label(label="", xalign=0)
        self.img_title.get_style_context().add_class("title")
        self.img_title.set_margin_bottom(round(self.font * 0.3))
        imgbox.pack_start(self.img_title, False, False, 0)
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=round(self.font * 1.0))
        self.img_frame = Gtk.EventBox()  # fondo blanco: el QR se lee mejor con margen claro
        self.img_frame.get_style_context().add_class("qr")
        self.img = Gtk.Image()
        self.img_frame.add(self.img)
        body.pack_start(self.img_frame, False, False, 0)
        self.img_lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=round(self.font * 0.35))
        self.img_lines.set_valign(Gtk.Align.CENTER)
        body.pack_start(self.img_lines, True, True, 0)
        imgbox.pack_start(body, False, False, 0)
        self.img_status = Gtk.Label(label="", xalign=0.5)
        self.img_status.get_style_context().add_class("msg")
        self.img_status.set_margin_top(round(self.font * 0.3))
        imgbox.pack_start(self.img_status, False, False, 0)
        self.img_hint = Gtk.Label(label="", xalign=0.5)
        self.img_hint.get_style_context().add_class("hint")
        imgbox.pack_start(self.img_hint, False, False, 0)
        self.stack.add_named(imgbox, "image")

        # Página "busy": spinner + texto
        busy = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=round(self.font * 0.6))
        busy.set_halign(Gtk.Align.CENTER)
        busy.set_margin_top(round(self.font * 0.3))
        busy.set_margin_bottom(round(self.font * 0.3))
        self.spinner = Gtk.Spinner()
        self.spinner.set_size_request(round(self.font * 1.3), round(self.font * 1.3))
        busy.pack_start(self.spinner, False, False, 0)
        self.busy_label = Gtk.Label(label="")
        self.busy_label.get_style_context().add_class("busy")
        busy.pack_start(self.busy_label, False, False, 0)
        self.stack.add_named(busy, "busy")

        # Página "toast": un aviso suelto (con el menú cerrado)
        self.toast_label = Gtk.Label(label="")
        self.toast_label.get_style_context().add_class("toast")
        self.toast_label.set_line_wrap(True)
        self.toast_label.set_justify(Gtk.Justification.CENTER)
        self.toast_label.set_max_width_chars(48)
        self.stack.add_named(self.toast_label, "toast")

        self.panel.show_all()
        self.qr_row.hide()

    # ------------------------------------------------------------ armado
    def _pass_through_mouse(self, *_a) -> None:
        """Que el puntero atraviese el panel (no tapa clics de la app de abajo)."""
        try:
            import cairo  # noqa: PLC0415
            gdk_win = self.win.get_window()
            if gdk_win is not None:
                gdk_win.input_shape_combine_region(cairo.Region(), 0, 0)
        except Exception:  # noqa: BLE001  (sin python3-cairo no pasa nada)
            pass

    def _icon(self, icon: str, name: str) -> Gtk.Widget:
        """Un archivo (SVG/PNG; sin carpeta = de tele_cec/icons), un ícono del tema, un emoji o la inicial."""
        if icon.lower().endswith((".svg", ".png", ".jpg", ".jpeg")):
            path = Path(os.path.expanduser(icon))
            if not path.is_absolute():
                path = ICONS_DIR / path
            try:
                pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), self.icon_px, self.icon_px, True)
                return Gtk.Image.new_from_pixbuf(pix)
            except GLib.Error as exc:
                print(f"menu_ui: no pude cargar el ícono {path}: {exc.message}", file=sys.stderr, flush=True)
        theme = Gtk.IconTheme.get_default()
        if icon and icon.isascii() and theme.has_icon(icon):
            img = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.DIALOG)
            img.set_pixel_size(self.icon_px)
            return img
        text = icon if icon and not icon.isascii() else (name[:1].upper() or "?")
        lbl = Gtk.Label(label=text)
        lbl.get_style_context().add_class("letter")
        lbl.set_size_request(self.icon_px, self.icon_px)
        return lbl

    def _set_level(self, i: int, it: dict) -> None:
        bar, pct, badge, row = self.levels[i]
        level = int(it.get("level", -1))
        bar.set_value(max(0, min(100, level)))
        pct.set_text(f"{level} %" if level >= 0 else "")
        (row.get_style_context().add_class if it.get("muted") else row.get_style_context().remove_class)("muted")
        if badge is not None:
            badge.set_text(str(it.get("badge") or ""))
            badge.set_visible(bool(it.get("badge")))
        icon = str(it.get("icon") or "")
        img = next((c for c in row.get_children() if isinstance(c, Gtk.Image)), None)
        if img is not None and icon and Gtk.IconTheme.get_default().has_icon(icon):
            img.set_from_icon_name(icon, Gtk.IconSize.DIALOG)
            img.set_pixel_size(self.icon_px)

    def _build_rows(self) -> None:
        for child in self.list_box.get_children():
            self.list_box.remove(child)
        self.rows = []
        self.levels: dict[int, tuple] = {}
        cols = self.columns if len(self.items) > 1 else 1
        per_col = -(-len(self.items) // cols)  # filas por columna (la primera se llena primero)
        grid = None
        if cols > 1:
            grid = Gtk.Grid(column_spacing=round(self.font * 0.5), row_spacing=round(self.font * 0.15))
            grid.set_column_homogeneous(True)
            self.list_box.pack_start(grid, False, False, 0)
        self.menu_page.set_size_request(round(self.width * (1 + 0.72 * (cols - 1))), -1)
        for i, it in enumerate(self.items):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=round(self.font * 0.55))
            ctx = row.get_style_context()
            ctx.add_class("row")
            if not it.get("enabled", True):
                ctx.add_class("off")
            if self.numbers:  # en el selector de carpetas no van (y no dejan el hueco)
                num = Gtk.Label(label=str(i + 1) if i < 9 else "")
                num.get_style_context().add_class("num")
                num.set_width_chars(2)
                row.pack_start(num, False, False, 0)
            row.pack_start(self._icon(str(it.get("icon") or ""), str(it.get("name") or "")), False, False, 0)
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            texts.set_valign(Gtk.Align.CENTER)
            name = Gtk.Label(label=str(it.get("name") or "?"), xalign=0)
            name.get_style_context().add_class("name")
            name.set_ellipsize(Pango.EllipsizeMode.END)
            # «en uso» / «sonando» al lado del nombre (ahí sobra lugar): la derecha queda para el botoncito.
            name_line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=round(self.font * 0.4))
            name_line.pack_start(name, False, True, 0)
            has_level = "level" in it  # la fila «Volumen»: porcentaje y barra
            badge = None
            if it.get("current") or it.get("badge") or has_level:
                badge = Gtk.Label(label=str(it.get("badge") or ("" if has_level else "en uso")))
                badge.get_style_context().add_class("badge")
                badge.set_valign(Gtk.Align.CENTER)
                name_line.pack_start(badge, False, False, 0)
                badge.set_no_show_all(not badge.get_label())
            texts.pack_start(name_line, False, False, 0)
            sub = Gtk.Label(label=str(it.get("sub") or ""), xalign=0)
            sub.get_style_context().add_class("sub")
            sub.set_ellipsize(Pango.EllipsizeMode.END)
            texts.pack_start(sub, False, False, 0)
            sub.set_no_show_all(not it.get("sub"))
            row.pack_start(texts, True, True, 0)
            if has_level:
                pct = Gtk.Label(label="", xalign=1)
                pct.get_style_context().add_class("pct")
                pct.set_width_chars(5)
                bar = Gtk.LevelBar(min_value=0, max_value=100, mode=Gtk.LevelBarMode.CONTINUOUS)
                for offset in (Gtk.LEVEL_BAR_OFFSET_LOW, Gtk.LEVEL_BAR_OFFSET_HIGH, Gtk.LEVEL_BAR_OFFSET_FULL):
                    bar.remove_offset_value(offset)  # sin los colores de «bajo / alto»: una sola barra
                bar.set_valign(Gtk.Align.CENTER)
                bar.set_margin_top(round(self.font * 0.2))
                texts.pack_start(bar, False, False, 0)
                row.pack_end(pct, False, False, 0)
                self.levels[i] = (bar, pct, badge, row)
                self._set_level(i, it)
            if it.get("action"):
                chip = Gtk.Label(label=str(it["action"]) + " ▸")
                chip.get_style_context().add_class("chip")
                chip.set_valign(Gtk.Align.CENTER)
                row.pack_end(chip, False, False, 0)
            if grid is not None:
                row.set_hexpand(True)
                grid.attach(row, i // per_col, i % per_col, 1, 1)
            else:
                self.list_box.pack_start(row, False, False, 0)
            self.rows.append(row)
        for child in self.qr_row.get_children():
            self.qr_row.remove(child)
        cell = self._qr_cell()
        if cell is not None and grid is not None and cols * per_col > len(self.items):
            cell.set_hexpand(True)
            grid.attach(cell, cols - 1, per_col - 1, 1, 1)  # el hueco de abajo a la derecha
        elif cell is not None:
            self.qr_row.pack_start(cell, False, False, 0)  # sin hueco: abajo, chico
        self.list_box.show_all()
        self.qr_row.show_all()
        self.qr_row.set_visible(bool(self.qr_row.get_children()))
        self.top = 0
        self._paint_selection()

    def _paint_selection(self) -> None:
        n = len(self.rows)
        chip = self.chip and 0 <= self.selected < n and bool(self.items[self.selected].get("action"))
        if self.columns > 1:  # en grilla se ve todo
            for i, row in enumerate(self.rows):
                ctx = row.get_style_context()
                (ctx.add_class if i == self.selected and not chip else ctx.remove_class)("sel")
                (ctx.add_class if i == self.selected and chip else ctx.remove_class)("near")
                row.set_visible(True)
            self.more_up.set_visible(False)
            self.more_down.set_visible(False)
            return
        # Ventana de MAX_ROWS filas que sigue a la selección.
        if self.selected < self.top:
            self.top = self.selected
        elif self.selected >= self.top + MAX_ROWS:
            self.top = self.selected - MAX_ROWS + 1
        self.top = max(0, min(self.top, max(0, n - MAX_ROWS)))
        for i, row in enumerate(self.rows):
            ctx = row.get_style_context()
            (ctx.add_class if i == self.selected and not chip else ctx.remove_class)("sel")
            (ctx.add_class if i == self.selected and chip else ctx.remove_class)("near")
            row.set_visible(self.top <= i < self.top + MAX_ROWS)
        long_list = n > MAX_ROWS
        self.more_up.set_visible(long_list)
        self.more_down.set_visible(long_list)
        self.more_up.set_opacity(1 if self.top > 0 else 0)          # sin cambiar el tamaño del panel
        self.more_down.set_opacity(1 if self.top + MAX_ROWS < n else 0)

    # ------------------------------------------------------------ teclado
    def _grab_keyboard(self, want: bool) -> None:
        """Tomar el teclado (o soltarlo) según la página: solo mientras se ve algo que se maneja con teclas."""
        want = want and self.keyboard
        if want == self._grabbing or not isinstance(self.win, Gtk.Window) or isinstance(self.win, Gtk.OffscreenWindow):
            return
        self._grabbing = want
        if self.layer_shell:
            if hasattr(GtkLayerShell, "set_keyboard_mode"):
                GtkLayerShell.set_keyboard_mode(self.win, GtkLayerShell.KeyboardMode.EXCLUSIVE if want
                                                else GtkLayerShell.KeyboardMode.NONE)
            else:
                GtkLayerShell.set_keyboard_interactivity(self.win, want)
        else:
            self.win.set_accept_focus(want)
            if want:
                self.win.present()

    def _on_key(self, _w, event) -> bool:
        name = keyboard_key(event.keyval)
        if name and self.mode in INTERACTIVE_PAGES:
            print(json.dumps({"key": name}), flush=True)
            return True
        return False

    def _on_key_release(self, _w, event) -> bool:
        if keyboard_key(event.keyval):
            print(json.dumps({"release": True}), flush=True)
            return True
        return False

    def _present(self, page: str) -> None:
        self._grab_keyboard(page in INTERACTIVE_PAGES)
        self.stack.set_visible_child_name(page)
        if page == "busy":
            self.spinner.start()
        else:
            self.spinner.stop()
        if isinstance(self.win, Gtk.Window) and not isinstance(self.win, Gtk.OffscreenWindow):
            self.win.resize(1, 1)  # que se achique o agrande según el contenido
        self.win.show()
        self.mode = page

    def _hide(self) -> None:
        self.spinner.stop()
        self._grab_keyboard(False)
        self.win.hide()
        self.mode = "hidden"

    def _keys_markup(self, keys: str) -> str:
        out = []
        for part in keys.split("  "):
            esc = GLib.markup_escape_text(part)
            color = KEY_COLORS.get(part)
            out.append(f'<span foreground="{color}">●</span> {esc}' if color else esc)
        return "   ".join(out)

    def _show_help(self, msg: dict) -> None:
        rows = [tuple(r) for r in (msg.get("rows") or [])]
        top = max(0, min(int(msg.get("top") or 0), max(0, len(rows) - MAX_HELP_ROWS)))
        self.help_title.set_text(str(msg.get("title") or "Ayuda"))
        self.help_page.set_text(str(msg.get("page") or ""))
        self.help_hint.set_text(str(msg.get("hint") or ""))
        for child in self.help_grid.get_children():
            self.help_grid.remove(child)
        for i, (keys, desc) in enumerate(rows[top:top + MAX_HELP_ROWS]):
            k = Gtk.Label(xalign=0)
            k.set_markup(self._keys_markup(str(keys)))
            k.get_style_context().add_class("help-keys")
            d = Gtk.Label(label=str(desc), xalign=0)
            d.get_style_context().add_class("help-desc")
            # Una sola línea: con ajuste de línea GTK pide una altura enorme y el panel se sale de la pantalla.
            d.set_ellipsize(Pango.EllipsizeMode.END)
            d.set_hexpand(True)
            self.help_grid.attach(k, 0, i, 1, 1)
            self.help_grid.attach(d, 1, i, 1, 1)
        long_list = len(rows) > MAX_HELP_ROWS
        self.help_up.set_visible(long_list)
        self.help_down.set_visible(long_list)
        self.help_up.set_opacity(1 if top > 0 else 0)
        self.help_down.set_opacity(1 if top + MAX_HELP_ROWS < len(rows) else 0)
        self.help_grid.show_all()
        self._toast_id += 1
        self._present("help")

    def _show_image(self, msg: dict) -> None:
        self.img_title.set_text(str(msg.get("title") or ""))
        size = round(self.font * float(msg.get("size") or 9))
        path = str(msg.get("path") or "")
        try:
            self.img.set_from_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True))
        except GLib.Error as exc:
            print(f"menu_ui: no pude cargar la imagen {path}: {exc.message}", file=sys.stderr, flush=True)
            self.img.clear()
        for child in self.img_lines.get_children():
            self.img_lines.remove(child)
        for line in msg.get("lines") or []:
            lbl = Gtk.Label(label=str(line), xalign=0)
            lbl.get_style_context().add_class("help-desc")
            lbl.set_line_wrap(True)  # pocas líneas al lado de una imagen de alto fijo: el alto no se dispara
            lbl.set_max_width_chars(34)
            self.img_lines.pack_start(lbl, False, False, 0)
        self.img_status.set_text(str(msg.get("status") or ""))
        self.img_hint.set_text(str(msg.get("hint") or ""))
        self.img_lines.show_all()
        self._toast_id += 1
        self._present("image")

    def _qr_cell(self) -> Gtk.Widget | None:
        """El QR de la página del celular, chico: el código y al lado «Desde el celular» y la dirección."""
        qr = self._qr or {}
        path = str(qr.get("path") or "")
        if not path:
            return None
        size = round(self.font * float(qr.get("size") or 2.9))
        try:
            pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True)
        except GLib.Error as exc:
            print(f"menu_ui: no pude cargar el QR {path}: {exc.message}", file=sys.stderr, flush=True)
            return None
        cell = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=round(self.font * 0.55))
        cell.get_style_context().add_class("qr-cell")
        cell.set_halign(Gtk.Align.END)     # contra el borde derecho (alineado con los botones «Conectar ▸»)
        cell.set_valign(Gtk.Align.CENTER)
        frame = Gtk.EventBox()  # fondo blanco: el QR se lee mejor con margen claro
        frame.get_style_context().add_class("qr")
        frame.set_valign(Gtk.Align.CENTER)
        frame.add(Gtk.Image.new_from_pixbuf(pix))
        cell.pack_start(frame, False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        texts.set_valign(Gtk.Align.CENTER)
        title = Gtk.Label(label=str(qr.get("title") or ""), xalign=0)
        title.get_style_context().add_class("sub")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        texts.pack_start(title, False, False, 0)
        url = Gtk.Label(label=str(qr.get("url") or ""), xalign=0)
        url.get_style_context().add_class("qr-url")
        texts.pack_start(url, False, False, 0)
        cell.pack_start(texts, False, False, 0)
        return cell

    def _set_hint(self, text: str, is_msg: bool = False) -> None:
        self.hint.set_text(text)
        ctx = self.hint.get_style_context()
        ctx.remove_class("hint" if is_msg else "msg")
        ctx.add_class("msg" if is_msg else "hint")

    # ------------------------------------------------------------ órdenes
    def handle(self, msg: dict) -> bool:
        op = msg.get("op")
        if op == "show":
            self.items = list(msg.get("items") or [])
            self.selected = max(0, min(int(msg.get("selected") or 0), len(self.items) - 1))
            self.title.set_text(str(msg.get("title") or "Actividades"))
            self.numbers = bool(msg.get("numbers", True))
            self.columns = max(1, min(int(msg.get("columns") or 1), 3))
            self.chip = bool(msg.get("chip"))
            self._hint = str(msg.get("hint") or DEFAULT_HINT)
            self._set_hint(self._hint)
            self._msg_id += 1
            self._toast_id += 1
            self._qr = msg.get("qr") or None
            self._build_rows()
            self._present("menu")
        elif op == "select":
            self.selected = max(0, min(int(msg.get("index") or 0), len(self.items) - 1))
            self.chip = bool(msg.get("chip"))
            self._paint_selection()
        elif op == "level":  # la barra de una fila («Volumen»), sin volver a armar la lista
            i = int(msg.get("index") or 0)
            if i in getattr(self, "levels", {}):
                self.items[i].update({k: msg.get(k) for k in ("level", "muted", "badge", "icon")})
                self._set_level(i, self.items[i])
        elif op == "help":
            self._show_help(msg)
        elif op == "image":
            self._show_image(msg)
        elif op == "status":  # cambia solo la línea de estado de la página "image"
            self.img_status.set_text(str(msg.get("text") or ""))
        elif op == "busy":
            self._toast_id += 1
            self.busy_label.set_text(str(msg.get("text") or "Un momento…"))
            self._present("busy")
        elif op == "toast":
            self._toast(str(msg.get("text") or ""), int(msg.get("ms") or 2000))
        elif op == "hide":
            self._toast_id += 1
            self._hide()
        elif op == "quit":
            Gtk.main_quit()
        return False  # para GLib.idle_add: una sola vez

    def _toast(self, text: str, ms: int) -> None:
        if self.mode == "menu":
            # Con el menú abierto el aviso va en la línea de ayuda y después vuelve la ayuda.
            self._msg_id += 1
            mid = self._msg_id
            self._set_hint(text, is_msg=True)

            def restore() -> bool:
                if mid == self._msg_id and self.mode == "menu":
                    self._set_hint(self._hint)
                return False
            GLib.timeout_add(ms, restore)
            return
        if self.mode == "busy":
            return
        self._toast_id += 1
        tid = self._toast_id
        self.toast_label.set_text(text)
        self._present("toast")

        def done() -> bool:
            if tid == self._toast_id and self.mode == "toast":
                self._hide()
            return False
        GLib.timeout_add(ms, done)


def _layer_shell_supported() -> bool:
    if GtkLayerShell is None:
        return False
    display = Gdk.Display.get_default()
    if display is None or "Wayland" not in type(display).__name__:
        return False
    is_supported = getattr(GtkLayerShell, "is_supported", None)
    return bool(is_supported()) if callable(is_supported) else True


def _read_stdin(panel: Panel) -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            print(f"menu_ui: orden inválida: {line[:120]}", file=sys.stderr, flush=True)
            continue
        if isinstance(msg, dict):
            GLib.idle_add(panel.handle, msg)
    GLib.idle_add(Gtk.main_quit)


def _demo(panel: Panel) -> None:
    steps: list[tuple[int, dict]] = [
        (0, {"op": "show", "items": DEMO_ITEMS, "selected": 0}),
        (1200, {"op": "select", "index": 1}),
        (2400, {"op": "select", "index": 2}),
        (3600, {"op": "select", "index": 3}),
        (4200, {"op": "toast", "text": "Sin fotos: conectá un pendrive", "ms": 1500}),
        (6000, {"op": "select", "index": 2}),
        (7000, {"op": "busy", "text": "Abriendo Videos…"}),
        (9500, {"op": "hide"}),
        (10000, {"op": "toast", "text": "Tidal no está instalado", "ms": 2000}),
        (12500, {"op": "quit"}),
    ]
    for ms, msg in steps:
        GLib.timeout_add(ms, panel.handle, msg)


def _capture(path: str, page: str) -> int:
    panel = Panel(offscreen=True)
    if page == "busy":
        panel.handle({"op": "show", "items": DEMO_ITEMS, "selected": 2})
        panel.handle({"op": "busy", "text": "Abriendo Videos…"})
    elif page == "toast":
        panel.handle({"op": "toast", "text": "Tidal no está instalado", "ms": 5000})
    elif page in ("sonido", "silencio"):
        mute = page == "silencio"
        panel.handle({"op": "show", "title": "Sonido", "numbers": False, "selected": 0,
                      "hint": "▲ ▼ elegir · Volumen y temporizador: ◀ ▶ cambiar · OK usar esa salida",
                      "items": [{"name": "Volumen", "icon": "audio-volume-muted" if mute else "audio-volume-high",
                                 "level": 40, "muted": mute, "badge": "en silencio" if mute else ""},
                                {"name": "Temporizador para apagar", "icon": "temporizador.svg",
                                 "sub": "Se apaga en 28 min · ◀ ▶ cambiarlo", "badge": "prendido"},
                                {"name": "TV (HDMI)", "icon": "video-display", "sub": "HDMI"},
                                {"name": "Auriculares BT", "icon": "bluetooth", "sub": "Bluetooth", "current": True}]})
        panel.handle({"op": "select", "index": 3 if mute else 1})
    else:
        panel.handle({"op": "show", "items": DEMO_ITEMS, "selected": 2})
    panel.win.show_all()

    def snap() -> bool:
        pix = panel.win.get_pixbuf()
        if pix is None:
            print("No pude capturar el panel", file=sys.stderr)
        else:
            pix.savev(path, "png", [], [])
            print(path)
        Gtk.main_quit()
        return False
    GLib.timeout_add(400, snap)
    Gtk.main()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tele_cec.ui.menu_ui", description=__doc__.splitlines()[0])
    ap.add_argument("--demo", action="store_true", help="mostrar el panel con opciones de ejemplo y salir")
    ap.add_argument("--captura", metavar="PNG", help="guardar una imagen del panel y salir")
    ap.add_argument("--pagina", choices=("menu", "busy", "toast", "sonido", "silencio"), default="menu", help="qué capturar")
    args = ap.parse_args(argv)
    if not Gtk.init_check(sys.argv)[0]:
        print("menu_ui: no hay sesión gráfica (WAYLAND_DISPLAY/DISPLAY)", file=sys.stderr, flush=True)
        return 1
    if args.captura:
        return _capture(args.captura, args.pagina)
    panel = Panel()
    if args.demo:
        print(f"layer-shell: {'sí' if panel.layer_shell else 'no (ventana común siempre encima)'}", flush=True)
        _demo(panel)
    else:
        print(json.dumps({"ready": True, "layer_shell": panel.layer_shell}), flush=True)
        threading.Thread(target=_read_stdin, args=(panel,), name="stdin", daemon=True).start()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
