# netflix-cec

Maneja **Netflix en Chromium o Firefox** con el **control remoto de la TV** (HDMI-CEC), pensado para una Raspberry Pi 4 / Pi 400 con Raspberry Pi OS Bookworm conectada a la TV.

No convierte el control en un teclado, como hace [cec-mini-kb](https://github.com/spleen1981/cec-mini-kb): entiende qué hay en pantalla y hace lo que corresponde.

- **Navegación espacial** con las flechas por perfiles, filas de títulos, fichas y menús, con un **resaltado bien visible** del elemento enfocado. OK = clic, Atrás = volver.
- **Botón inteligente** (amarillo): salta la intro o el resumen, o pasa al siguiente episodio, según lo que muestre Netflix.
- **Perfiles y "Seguir viendo"**: elegís el perfil con los números y retomás lo último visto con un botón (verde).
- **Reproducción**: play/pausa, ±10 s, volumen, y ciclar subtítulos y audio, con avisos en pantalla.
- **Modo aprendizaje**: muestra en consola el código y el nombre de cada tecla CEC que llega.
- **Servicio de usuario de systemd** con logs en journald. Se recupera solo si se cierra el navegador o se cae el adaptador CEC.

## Cómo funciona

```
Control remoto ──IR──▶ TV ──HDMI-CEC──▶ cec-client (libcec) ──▶ netflix-cec ──DevTools o WebDriver BiDi (127.0.0.1:9222)──▶ Chromium o Firefox + Netflix
```

1. **CEC**: `netflix-cec` ejecuta `cec-client` (paquete `cec-utils`, la misma libcec que usan Kodi y cec-mini-kb) y lee el tráfico CEC. La TV manda `USER_CONTROL_PRESSED` (0x44) con el código de la tecla, que es el mismo de `map.txt` de cec-mini-kb (0 = OK, 1–4 = flechas, 13 = Exit, 113–116 = colores, 32–41 = números…). Si `cec-client` se cae, se reinicia solo.
2. **Contexto**: antes de cada tecla, el servicio le pregunta a la página dónde está: perfiles, inicio/fichas, reproduciendo, un menú sobre el reproductor, un campo de texto, o el navegador cerrado. La misma tecla hace cosas distintas según el contexto: por ejemplo, la flecha derecha navega en el inicio y adelanta 10 s en el reproductor.
3. **Navegador**: Chromium (o Firefox, ver [Firefox](#firefox)) se abre con el puerto de control solo en `127.0.0.1`: DevTools en Chromium, WebDriver BiDi en Firefox. Por ahí el servicio:
   - inyecta un script con la navegación espacial, el resaltado y los avisos;
   - hace clics "reales" con el mouse, que Netflix acepta igual que los de una persona;
   - controla el reproductor con la API interna de Netflix. Si no está disponible, usa los atajos de teclado de Netflix.

## Requisitos

- Raspberry Pi OS Bookworm (o Debian/Ubuntu) con escritorio, Wayland o X11.
- Chromium con Netflix funcionando (`libwidevinecdm0`), o Firefox con Netflix funcionando (ver [Firefox](#firefox)).
- `/dev/cec0` (lo crea el driver `vc4-kms-v3d`, que viene activo por defecto).
- Python ≥ 3.11 (Bookworm trae 3.11), `cec-utils` y `python3-websockets`. El instalador los instala si faltan.

## Instalación

```bash
# copiá esta carpeta a la Pi (git clone, scp, pendrive…) y dentro de ella:
./install.sh --copiar-perfil
```

| Opción | Qué hace |
|---|---|
| `--firefox` | Usa Firefox en vez de Chromium: deja `engine = "firefox"` en `[browser]` de tu configuración. |
| `--copiar-perfil` | Copia tu perfil de Chromium (`~/.config/chromium`) al perfil propio de netflix-cec (`~/.config/chromium-netflix`), así no tenés que volver a iniciar sesión en Netflix. **Cerrá Chromium antes.** Funciona porque usás `--password-store=basic`. Con `--firefox` copia tu perfil de Firefox a `~/.config/firefox-netflix`. |
| `--sin-apt` | No instala paquetes, solo avisa si falta algo. |
| `--sin-servicio` | Instala todo pero no habilita el servicio. |

El instalador:
- copia el programa a `~/.local/share/netflix-cec`;
- crea el comando `~/.local/bin/netflix-cec`;
- crea la configuración `~/.config/netflix-cec/config.toml` (si no existe) y el acceso **"Netflix (TV)"** en el menú;
- habilita el servicio `netflix-cec` de systemd (usuario).

Si te agrega al grupo `video` para usar `/dev/cec0`, reiniciá la Pi.

> **¿Por qué un perfil de Chromium aparte?** Desde la versión 136, Chromium no permite el puerto de depuración (DevTools) sobre el perfil por defecto. Por eso netflix-cec usa `~/.config/chromium-netflix`. Tu Chromium de siempre no cambia.

## Primer uso

1. Cerrá Kodi: dos programas no pueden usar CEC a la vez.
2. Mirá qué manda tu control:
   ```bash
   netflix-cec aprender
   ```
   Apretá botones: vas a ver el nombre, el código y qué hace cada uno. Si el servicio estaba corriendo, el comando lo detiene mientras tanto y lo vuelve a iniciar al salir con Ctrl+C.
3. Abrí Netflix: apretá **OK** en el control (con el navegador cerrado lo abre en pantalla completa) o usá **"Netflix (TV)"** en el menú. La primera vez iniciá sesión, salvo que hayas usado `--copiar-perfil`.
4. Verificá que todo esté bien:
   ```bash
   netflix-cec diagnostico
   ```

## Teclas por defecto

Si una tecla no está definida en el contexto actual, se busca en el siguiente: `profiles → browse → global`, `player → global`, `player_menu → browse → global`.

| Tecla | Inicio / fichas (`browse`) | Reproduciendo (`player`) |
|---|---|---|
| Flechas | Moverse (con resaltado) | ◀ ▶ −/+10 s · ▲ ▼ volumen |
| OK | Clic (la 1.ª vez solo muestra el resaltado) | Play / pausa |
| Exit / Return | Volver · cerrar ficha · subir al principio | Salir del reproductor |
| 🟡 Amarillo / ⏭ Forward | Botón inteligente | Botón inteligente |
| 🟢 Verde | Seguir viendo | Seguir viendo |
| 🔴 Rojo | Cambiar de perfil | Ciclar audio |
| 🔵 Azul | Ir al inicio | Ciclar subtítulos |
| ▶ Play / ⏯ Play-Pausa | Reproducir el título resaltado o el de la ficha abierta | Play / pausa |
| ⏪ ⏩ · ⏮ Backward | — | −/+10 s · −10 s |
| Canal + | — | Siguiente episodio |
| 0 | — | Modo navegación (flechas para moverse por los controles) |

Al abrirse una ficha, el resaltado pasa solo a su botón **Reproducir/Reanudar** (OK dos veces = abrir y reproducir), y al cerrarla vuelve a la tarjeta.

Otros contextos:
- **"¿Quién está viendo?"** (`profiles`): los números **1–5** eligen el perfil; también funcionan las flechas + OK.
- **Menú sobre el reproductor** (`player_menu`): se detecta solo (selector de audio/subtítulos, episodios, pantalla final…) y las flechas navegan. Con **0** se fuerza este modo.
- **Campo de texto** (`text`, por ejemplo un PIN): los números escriben dígitos.
- **Navegador cerrado** (`offline`): **OK** abre Netflix y **verde** lo abre y retoma "Seguir viendo".

> Las teclas de **volumen de la TV** casi nunca viajan por CEC: controlan la TV directamente, como siempre. El "volumen" de la tabla es el del reproductor de Netflix.

### Botón inteligente

Busca en pantalla, en orden: **Saltar intro**, **Saltar resumen**, **Saltar** (avance) y **Siguiente episodio**, y aprieta el primero que encuentra. Si no hay nada, avisa "Nada para saltar". Si lo apretás otra vez dentro de 4 s, pasa al **siguiente episodio**.

### Perfiles y "Seguir viendo"

- **Rojo** abre "¿Quién está viendo?" y ahí elegís con los números.
- **Verde** va al inicio, busca la fila "Seguir viendo" y abre el primer título.
  - Si aparece "¿Quién está viendo?", te pide que elijas un perfil, o elige solo el de `default_profile` si lo configuraste.
- Para que un solo botón haga todo, usá una macro (ver abajo).

### Avisos mientras trabaja

- **Indicador "trabajando"**: si una acción tarda más de un cuarto de segundo (▶ en la lista, verde, perfiles, inicio…), arriba a la derecha aparece un circulito con lo que está haciendo: «Abriendo «Merlina»…» → «Cargando «Merlina»…». No se apaga al cambiar de página: sigue hasta que arranca el video o termina de cargar la pantalla (como máximo 15–20 s). **Atrás** lo cancela.
- **Pantalla de carga**: al abrir el navegador se ve «Abriendo Netflix…» en vez de una pantalla vacía, y después el indicador hasta que Netflix termina de cargar (`browser.splash`).
- **Eco de teclas** (apagado): con `feedback.key_echo = true`, cada tecla que llega se muestra un instante arriba a la izquierda. Sirve para saber si la TV no mandó la tecla o si llegó y la acción no hizo nada.
- **Aviso en la TV** (apagado): con `feedback.cec_osd = true`, al abrir el navegador se le pide a la TV por CEC que muestre "Netflix...". Muchas TVs lo ignoran: probalo con la tuya.
- En el log, las acciones que tardan un segundo o más quedan registradas con su duración (`netflix: continue_watching: 2.3 s`).

### Mantener apretado

Las flechas, los ±10 s y el volumen se repiten mientras mantenés el botón, si tu TV repite la tecla. Las macros, los perfiles y el botón inteligente se ejecutan una sola vez por pulsación. Para distinguir "mantener" de "apretar dos veces" se usa la liberación de la tecla (CEC 0x45); si tu TV no la manda, se usa `debounce_ms`.

## Firefox

netflix-cec también funciona con Firefox:

```bash
./install.sh --firefox                  # o --firefox --copiar-perfil para no volver a iniciar sesión
```

O, en tu `config.toml`:

```toml
[browser]
engine = "firefox"
```

Funciona igual que con Chromium (mismas teclas, contextos y acciones). Cambia esto:

- **Protocolo**: Firefox ya no tiene DevTools (CDP). Se controla por **WebDriver BiDi**, el estándar que lo reemplaza, por el mismo `--remote-debugging-port`. Los clics y las teclas también llegan como "reales".
- **Una sola sesión de control**: Firefox admite un solo cliente a la vez, y lo es el servicio. Por eso `accion`, `diagnostico` y `navegador` se lo piden al servicio por su socket local. Si el servicio no está corriendo, se conectan por su cuenta y cierran la sesión al terminar.
- **Sesión colgada**: si un cliente muere sin cerrar la sesión (por ejemplo, con `kill -9`), Firefox no la libera nunca. El servicio lo detecta y reinicia ese Firefox, el del perfil `firefox-netflix`. Tu sesión de Netflix se conserva porque está en el perfil.
- **Arranque lento** (Raspberry Pi): Firefox abre el puerto de control enseguida, pero recién acepta la sesión cuando terminó de abrir la ventana, y eso puede tardar. netflix-cec espera hasta 90 s, y si deja de esperar antes, igual cierra esa sesión apenas Firefox responde, para que no quede tomada.
- **Perfil y preferencias**: el perfil es `~/.config/firefox-netflix`. Cada vez que abre Firefox, netflix-cec le escribe un `user.js` con lo necesario: DRM, reproducción automática, sin carteles de primer inicio ni de restaurar sesión, y sin el botón de imagen en imagen sobre el video. También desactiva las "preferencias recomendadas" del agente remoto, que son para pruebas automáticas (apagan Safe Browsing, el guardado de contraseñas y la protección contra rastreo). Agregá las tuyas en `[browser.firefox] prefs`.
- **`navigator.webdriver`**: con el control remoto activo, Firefox le avisa a las páginas que está automatizado (Chromium no). netflix-cec lo oculta para que Netflix vea el mismo navegador de siempre. Se puede desactivar con `[browser.firefox] hide_webdriver = false`.
- **Barra a rayas con un robot**: es el aviso de Firefox de que está bajo control remoto. En modo kiosko no se ve.

```toml
[browser.firefox]
binary = ""                              # "" = firefox / firefox-esr
profile = "~/.config/firefox-netflix"
extra_args = []
prefs = { "media.ffmpeg.vaapi.enabled" = true }
hide_webdriver = true
```

## Configuración

Archivo: `~/.config/netflix-cec/config.toml`. Poné **solo lo que quieras cambiar**: lo demás toma el valor por defecto. Todos los valores por defecto están comentados en `~/.local/share/netflix-cec/netflix_cec/defaults.toml`.

Después de editar:

```bash
systemctl --user restart netflix-cec
```

Ejemplo:

```toml
[cec]
port = "/dev/cec1"          # TV en el HDMI 1 de la Pi 400

[browser]
launch = "always"           # Netflix siempre abierto, como un aparato de TV

[netflix]
default_profile = 1         # "Seguir viendo" elige el perfil 1 si hace falta

[keys.player]
LEFT = "seek:-30"           # izquierda = −30 s

[keys.browse]
NUMBER9 = ["profile:1", "wait:2500", "continue_watching"]   # macro en línea
F4_YELLOW = ""              # desactivar una tecla en ese contexto

[macros]
noche = ["profile:2", "wait:2500", "continue_watching"]

[keys.global]
F1_BLUE = "macro:noche"
```

Las teclas se nombran como en libcec (`UP`, `SELECT`, `EXIT`, `F2_RED`, `NUMBER1`, `CHANNEL_UP`…) o con su número (`"113"`, `"0x71"`). `netflix-cec aprender` te muestra ambos.

### Opciones principales

| Clave | Por defecto | Para qué |
|---|---|---|
| `cec.port` | `""` (autodetectar) | `/dev/cec0` = HDMI 0, `/dev/cec1` = HDMI 1 |
| `cec.device_type` | `"r"` | Tipo de dispositivo CEC: `r` recording (como cec-mini-kb) o `p` playback |
| `cec.activate_source` | `"both"` | Cuándo anunciarse como fuente activa (la TV cambia a la Pi): `start`, `launch`, `both`, `never` |
| `cec.pause_on_standby` | `true` | Pausar si se apaga la TV |
| `browser.engine` | `"chromium"` | `chromium` o `firefox` (ver [Firefox](#firefox)) |
| `browser.launch` | `"on_key"` | Navegador cerrado: `on_key` (OK lo abre), `always` (siempre abierto), `never` |
| `browser.user_data_dir` | `~/.config/chromium-netflix` | Perfil de Chromium propio |
| `browser.firefox.profile` | `~/.config/firefox-netflix` | Perfil de Firefox propio |
| `browser.extra_args` | `["--password-store=basic"]` | Flags extra de Chromium (por ejemplo tu `--user-agent`) |
| `browser.kiosk` | `true` | Pantalla completa sin barras |
| `browser.reduce_effects` | `true` | Sin animaciones, transiciones ni scroll suave (ver [Rendimiento](#rendimiento)) |
| `browser.stop_previews` | `true` | Pausar y ocultar los avances que Netflix reproduce solos en el inicio |
| `browser.splash` | `true` | Pantalla «Abriendo Netflix…» mientras arranca el navegador |
| `feedback.busy` · `feedback.busy_delay_ms` | `true` · `250` | Indicador "trabajando" y a partir de cuánto mostrarlo (ver [Avisos](#avisos-mientras-trabaja)) |
| `feedback.key_echo` | `false` | Mostrar cada tecla recibida |
| `feedback.cec_osd` · `feedback.cec_osd_text` | `false` · `"Netflix..."` | Texto en la pantalla de la TV al abrir el navegador (máx. 13 caracteres) |
| `netflix.seek_seconds` | `10` | Salto de ◀ ▶ |
| `netflix.volume_step` | `0.1` | Paso de volumen (10 %) |
| `netflix.default_profile` | `0` | Perfil para "Seguir viendo" (0 = preguntar) |

### Acciones

| Acción | Qué hace |
|---|---|
| `nav:up` `nav:down` `nav:left` `nav:right` | Mover el resaltado (en las filas pasa de página solo) |
| `ok` | Clic en el elemento resaltado |
| `back` | Volver: cierra la ficha o el menú, sale del reproductor o vuelve atrás |
| `smart_skip` | Botón inteligente |
| `continue_watching` | Retomar lo último de "Seguir viendo" |
| `profiles` · `profile:N` | Abrir "¿Quién está viendo?" · elegir el perfil N |
| `home` | Ir al inicio |
| `play_pause` `play` `pause` | Reproducción. En `browse`, `play` y `play_pause` reproducen el título resaltado (abren su ficha y aprietan «Reproducir») o el de la ficha abierta |
| `seek:+10` `seek:-30` | Saltar segundos |
| `volume:+` `volume:-` `mute` | Volumen del reproductor |
| `subtitles` · `audio` | Ciclar subtítulos (incluye "desactivados") · ciclar idioma de audio |
| `next_episode` | Siguiente episodio |
| `fullscreen` | Pantalla completa de Netflix (tecla F) |
| `nav_mode` | Alternar navegación con flechas dentro del reproductor |
| `type:123` | Escribir texto |
| `key:Escape` | Mandar una tecla (Escape, Enter, Space, flechas, letras…) |
| `toast:Hola` | Mostrar un aviso en pantalla |
| `wait:1500` | Esperar (milisegundos), útil en macros |
| `launch` | Abrir el navegador en Netflix |
| `reload` | Recargar la página |
| `macro:nombre` | Ejecutar una macro de `[macros]` |
| `none` o `""` | Nada (desactiva la tecla) |

Una **macro** es una lista de acciones. Si apretás cualquier tecla mientras una macro está corriendo, la macro se cancela.

## Comandos

```bash
netflix-cec aprender [--todo] [--crudo]   # ver las teclas CEC (--todo: todo el tráfico; --crudo: salida de cec-client)
netflix-cec diagnostico [--detalle]       # revisar CEC, el navegador y la página actual de Netflix
netflix-cec navegador [URL]               # abrir el navegador listo para el control (o traerlo al frente)
netflix-cec accion ACCIÓN [ACCIÓN…]       # ejecutar acciones ya mismo, p. ej.: netflix-cec accion smart_skip
netflix-cec servicio [--teclado]          # el servicio (lo usa systemd); --teclado = probar escribiendo teclas
netflix-cec --debug …                     # cualquiera de los anteriores con logs detallados
```

`netflix-cec accion` sirve para probar desde otra máquina por SSH, por ejemplo `netflix-cec accion nav:down ok`.

Si el servicio está corriendo, `accion`, `diagnostico` y `navegador` se lo piden a él por un socket local (`$XDG_RUNTIME_DIR/netflix-cec.sock`), así que el detalle de lo que pasó queda en su log. Con Chromium, `accion --directo` se conecta al navegador por su cuenta, útil con `--debug` para ver cada paso en la consola. Con Firefox no se puede, porque admite una sola sesión de control.

## Servicio y logs

```bash
systemctl --user status netflix-cec
systemctl --user restart netflix-cec
journalctl --user -u netflix-cec -f            # en vivo
journalctl --user -u netflix-cec -p warning    # solo avisos y errores
```

El log registra cada tecla con su contexto y acción:

```
cec: CEC listo (/dev/cec0)
netflix: Conectado a Chromium: https://www.netflix.com/browse
netflix: Tecla F3_GREEN (verde) [browse] -> continue_watching
netflix: Seguir viendo: Merlina
netflix: Tecla F4_YELLOW (amarillo) [player] -> smart_skip
netflix: Botón inteligente: Saltar intro
```

El servicio arranca con tu sesión (`default.target`). Con el inicio automático de sesión de Raspberry Pi OS, arranca al encender la Pi.

### Recuperación automática

| Qué pasa | Qué hace netflix-cec |
|---|---|
| `cec-client` termina o no logra abrir el adaptador | Lo reinicia con espera creciente (2 s → 60 s) |
| Desaparece `/dev/cecN` o hay una ráfaga de errores CEC | Reinicia `cec-client` |
| `cec-client` se cuelga al arrancar (sin "waiting for input" en 45 s) | Lo mata y reinicia |
| Se cierra o se cae el navegador | Vuelve al contexto `offline`, reintenta conectarse cada 2 s y lo reabre con OK, o solo si `launch = "always"` (máximo una vez cada 15 s; tras 5 fallos seguidos espera 5 min) |
| Se recarga la página o Netflix navega | Vuelve a inyectar el script |
| El propio servicio falla | systemd lo reinicia (`Restart=always`) |

| Firefox quedó con la sesión de control tomada (un cliente murió sin cerrarla) | Firefox no la libera nunca: el servicio reinicia ese Firefox, el del perfil `firefox-netflix` (salvo con `launch = "never"`) |

Reiniciar o detener el servicio **no cierra el navegador**, porque se abre en su propio scope de systemd.

## Rendimiento

En una Raspberry Pi, Netflix en Chromium puede andar pesado. netflix-cec trae activadas dos opciones que lo alivianan:

- **`reduce_effects = true`**:
  - Abre Chromium con `--force-prefers-reduced-motion` y `--disable-smooth-scrolling` (en Firefox: `ui.prefersReducedMotion` y `general.smoothScroll` en el `user.js`).
  - Inyecta CSS que deja todas las transiciones y animaciones de la página en 1 ms. Las animaciones quedan en 1 ms y no en "nada" para que el código de Netflix que espera el final de una animación (por ejemplo, el paso de página de las filas) siga funcionando.
  - Ese CSS también saca lo que más le cuesta dibujar a la GPU de la Pi: desenfoques (`backdrop-filter`, `filter`), sombras difusas (`box-shadow`, `text-shadow`), capas extra (`will-change`) y fondos fijos. El marco de foco queda con un contorno sólido, sin resplandor.
  - Las animaciones que Netflix hace por JavaScript con `element.animate()` (el CSS no las alcanza) también pasan a durar 1 ms.
  - El resaltado y el scroll pasan a ser instantáneos.
  - `netflix-cec diagnostico` muestra si todo esto está activo y qué animaciones siguen corriendo en la página en ese momento.

Independientemente de esta opción, el marco de foco solo sigue al elemento cuadro a cuadro un instante después de cada tecla o scroll; el resto del tiempo se revisa 4 veces por segundo. Al pasar de página una fila o al cerrar una ficha con Atrás no hay esperas fijas: la página avisa apenas la fila dejó de moverse o la ficha se cerró (sin animaciones, unos 100 ms en vez de 250–350 ms). Si preferís una espera fija para el paso de página, usá `netflix.page_delay_ms`. Y si mantenés apretado un botón mientras la página viene lenta, las repeticiones que quedaron esperando más de 0,35 s se descartan, para que el foco no siga de largo después de soltarlo.
- **`stop_previews = true`**: pausa y oculta los videos que Netflix reproduce solos mientras navegás (el avance del inicio y las vistas previas). No afecta al reproductor.

Para volver a los efectos normales, en tu `config.toml`:

```toml
[browser]
reduce_effects = false
stop_previews = false
```

Los flags se aplican al abrir el navegador y el CSS al reiniciar el servicio, así que después del cambio: `systemctl --user restart netflix-cec`, cerrá el navegador y volvé a abrirlo (OK en el control). En Firefox, al reabrirlo netflix-cec también borra esas preferencias de `prefs.js`, donde Firefox las había guardado, para que vuelvan a su valor normal.

Otras cosas que ayudan bastante:

- **Fuente de alimentación**: con una fuente o un cable flojos, la Pi detecta baja tensión y frena la CPU (una Pi 400 pasa de 1800 a 600 MHz), y Netflix se vuelve lentísimo: tarda más de medio minuto en calmarse después de cargar el inicio y las teclas responden con segundos de demora. `netflix-cec diagnostico` lo muestra en "Alimentación". Usá la fuente oficial (5,1 V / 3 A, USB-C) con su cable.

- **Desactivar los avances desde Netflix**: en tu cuenta → Perfiles → (tu perfil) → Configuración de reproducción, destildá "Reproducir avances automáticamente mientras navegas en todos los dispositivos". Es más efectivo que `stop_previews`, porque Netflix ni siquiera los descarga.
- **Resolución 1080p, no 4K**: si la TV es 4K y el escritorio corre a 3840×2160, la Pi dibuja cuatro veces más píxeles. Configurá 1920×1080 en *Preferencias → Configuración de pantalla*. Netflix en Chromium para Linux reproduce como mucho en 1080p (normalmente 720p), así que no perdés calidad de video.
- **Aceleración por GPU**: abrí `chrome://gpu` en Chromium y verificá que diga "Hardware accelerated" en la mayoría de los ítems.

## Solución de problemas

**`aprender` no muestra teclas**
- Cerrá Kodi y elegí en la TV la entrada HDMI de la Pi.
- En la TV, activá HDMI-CEC (Anynet+, Simplink, Bravia Sync, EasyLink…).
- Probá `device_type = "p"` en `[cec]`.
- Si la TV está en el otro HDMI de la Pi 400, probá `port = "/dev/cec1"`.
- Corré `netflix-cec aprender --crudo` para ver la salida completa de `cec-client`.

**Algunas teclas aparecen como "Vendor"**: tu TV las manda con un comando propio del fabricante, no con el estándar. Esas teclas no se pueden mapear todavía.

**La TV cambia sola a la Pi al encenderla**: poné `activate_source = "launch"` o `"never"`.

**Chromium no abre**
- Revisá el log y `netflix-cec diagnostico`.
- Si ya hay otro Chromium usando `~/.config/chromium-netflix` sin el puerto de depuración, cerralo.
- Para usar tu propio acceso directo, tiene que incluir `--remote-debugging-port=9222 --user-data-dir=$HOME/.config/chromium-netflix`. Lo más simple es usar "Netflix (TV)" o `netflix-cec navegador`.

**Netflix no reproduce en el perfil nuevo** (error de DRM o de navegador no compatible): agregá en `browser.extra_args` los mismos flags que usás en tu Chromium habitual, por ejemplo tu `--user-agent`. En Firefox, la primera vez Widevine se descarga solo y tarda un minuto: esperá y probá de nuevo.

**Quiero salir de la pantalla completa**: con `kiosk = true` (por defecto) el navegador no tiene barras y F11 no hace nada. Desde el teclado: **Alt+Tab** cambia a otra ventana y **Alt+F4** cierra el navegador (el control lo vuelve a abrir con OK). Para usarlo en ventana poné `kiosk = false` en `[browser]`: Chromium abre en pantalla completa común (F11 la alterna) y Firefox abre en una ventana normal.

**Algo de Netflix dejó de funcionar** (el botón inteligente no encuentra nada, no detecta los perfiles…): Netflix cambia su web cada tanto.
1. Con esa pantalla abierta, corré `netflix-cec diagnostico --detalle`. Lista los atributos `data-uia` visibles y los métodos del reproductor.
2. Ajustá el selector correspondiente en `[selectors]` de tu `config.toml`. La lista completa está en `netflix_cec/config.py` (`DEFAULT_SELECTORS`).

**El resaltado se pierde o salta raro**: corré `netflix-cec --debug accion nav:down` y `netflix-cec diagnostico --detalle` para ver qué elementos considera navegables. Lo que no deba enfocarse se agrega a `selectors.ignore`.

## Seguridad

El puerto de control (DevTools o WebDriver BiDi) escucha **solo en 127.0.0.1**. Cualquier programa que corra en la Pi con tu usuario podría controlar ese navegador, igual que podría leer tus archivos. No abras ese puerto a la red (no uses `--remote-debugging-address=0.0.0.0`). El socket del servicio está en `$XDG_RUNTIME_DIR`, con permisos solo para tu usuario. Los perfiles `chromium-netflix` y `firefox-netflix` son solo para Netflix.

## Probar sin TV ni Netflix

```bash
python3 tests/e2e_mock.py                    # navegador headless + Netflix simulado + teclas CEC simuladas (76 verificaciones por navegador)
python3 tests/e2e_mock.py --layout nuevo     # solo el diseño actual de Netflix (o --layout viejo)
python3 tests/e2e_mock.py --motor firefox    # solo Firefox (o --motor chromium); por defecto, los que estén instalados
python3 tests/serve_mock.py                  # sirve el Netflix simulado en http://www.netflix.com.localhost:8765/browse (?layout=viejo para el diseño anterior)
netflix-cec servicio --teclado               # escribir teclas por consola: u d l r ok b rojo verde amarillo azul 1..9
```

`tests/fake_cec_client.py` imita a `cec-client`: manda las teclas de `FAKE_KEYS` y puede "caerse" con `FAKE_CRASH_AFTER`. Sirve para probar la recuperación.

## Desinstalar

```bash
./uninstall.sh
```

La configuración y los perfiles `chromium-netflix` y `firefox-netflix` se conservan.

## Créditos

- Los códigos de tecla CEC y la forma de leer libcec (tipo *recording device*, descartar las liberaciones como pulsaciones) salen de [cec-mini-kb](https://github.com/spleen1981/cec-mini-kb).
- CEC: [libcec](https://github.com/Pulse-Eight/libcec) de Pulse-Eight.
