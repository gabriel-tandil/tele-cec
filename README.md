# netflix-cec

Maneja **Netflix en Chromium** con el **control remoto de la TV** (HDMI-CEC), pensado para una Raspberry Pi 4 / Pi 400 con Raspberry Pi OS Bookworm conectada a la TV.

No convierte el control en un teclado, como hace [cec-mini-kb](https://github.com/spleen1981/cec-mini-kb): entiende qué hay en pantalla y hace lo que corresponde.

- **Navegación espacial** con las flechas por perfiles, filas de títulos, fichas y menús, con un **resaltado bien visible** del elemento enfocado. OK = clic, Atrás = volver.
- **Botón inteligente** (amarillo): salta la intro o el resumen, o pasa al siguiente episodio, según lo que muestre Netflix.
- **Perfiles y "Seguir viendo"**: elegís el perfil con los números y retomás lo último visto con un botón (verde).
- **Reproducción**: play/pausa, ±10 s, volumen, y ciclar subtítulos y audio, con avisos en pantalla.
- **Modo aprendizaje**: muestra en consola el código y el nombre de cada tecla CEC que llega.
- **Servicio de usuario de systemd** con logs en journald. Se recupera solo si se cierra Chromium o se cae el adaptador CEC.

## Cómo funciona

```
Control remoto ──IR──▶ TV ──HDMI-CEC──▶ cec-client (libcec) ──▶ netflix-cec ──DevTools (127.0.0.1:9222)──▶ Chromium + Netflix
```

1. **CEC**: `netflix-cec` ejecuta `cec-client` (paquete `cec-utils`, la misma libcec que usan Kodi y cec-mini-kb) y lee el tráfico CEC. La TV manda `USER_CONTROL_PRESSED` (0x44) con el código de la tecla, que es el mismo de `map.txt` de cec-mini-kb (0 = OK, 1–4 = flechas, 13 = Exit, 113–116 = colores, 32–41 = números…). Si `cec-client` se cae, se reinicia solo.
2. **Contexto**: antes de cada tecla, el servicio le pregunta a la página dónde está: perfiles, inicio/fichas, reproduciendo, un menú sobre el reproductor, un campo de texto, o Chromium cerrado. La misma tecla hace cosas distintas según el contexto: por ejemplo, la flecha derecha navega en el inicio y adelanta 10 s en el reproductor.
3. **Chromium**: Chromium se abre con el puerto de depuración (DevTools) solo en `127.0.0.1`. Por ahí el servicio:
   - inyecta un script con la navegación espacial, el resaltado y los avisos;
   - hace clics "reales" con el mouse, que Netflix acepta igual que los de una persona;
   - controla el reproductor con la API interna de Netflix. Si no está disponible, usa los atajos de teclado de Netflix.

## Requisitos

- Raspberry Pi OS Bookworm (o Debian/Ubuntu) con escritorio, Wayland o X11.
- Chromium con Netflix funcionando (`libwidevinecdm0`).
- `/dev/cec0` (lo crea el driver `vc4-kms-v3d`, que viene activo por defecto).
- Python ≥ 3.11 (Bookworm trae 3.11), `cec-utils` y `python3-websockets`. El instalador los instala si faltan.

## Instalación

```bash
# copiá esta carpeta a la Pi (git clone, scp, pendrive…) y dentro de ella:
./install.sh --copiar-perfil
```

| Opción | Qué hace |
|---|---|
| `--copiar-perfil` | Copia tu perfil de Chromium (`~/.config/chromium`) al perfil propio de netflix-cec (`~/.config/chromium-netflix`), así no tenés que volver a iniciar sesión en Netflix. **Cerrá Chromium antes.** Funciona porque usás `--password-store=basic`. |
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
3. Abrí Netflix: apretá **OK** en el control (con Chromium cerrado lo abre en pantalla completa) o usá **"Netflix (TV)"** en el menú. La primera vez iniciá sesión, salvo que hayas usado `--copiar-perfil`.
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
| Play / Pause | Play / pausa | Play / pausa |
| ⏪ ⏩ · ⏮ Backward | — | −/+10 s · −10 s |
| Canal + | — | Siguiente episodio |
| 0 | — | Modo navegación (flechas para moverse por los controles) |

Otros contextos:
- **"¿Quién está viendo?"** (`profiles`): los números **1–5** eligen el perfil; también funcionan las flechas + OK.
- **Menú sobre el reproductor** (`player_menu`): se detecta solo (selector de audio/subtítulos, episodios, pantalla final…) y las flechas navegan. Con **0** se fuerza este modo.
- **Campo de texto** (`text`, por ejemplo un PIN): los números escriben dígitos.
- **Chromium cerrado** (`offline`): **OK** abre Netflix y **verde** lo abre y retoma "Seguir viendo".

> Las teclas de **volumen de la TV** casi nunca viajan por CEC: controlan la TV directamente, como siempre. El "volumen" de la tabla es el del reproductor de Netflix.

### Botón inteligente

Busca en pantalla, en orden: **Saltar intro**, **Saltar resumen**, **Saltar** (avance) y **Siguiente episodio**, y aprieta el primero que encuentra. Si no hay nada, avisa "Nada para saltar". Si lo apretás otra vez dentro de 4 s, pasa al **siguiente episodio**.

### Perfiles y "Seguir viendo"

- **Rojo** abre "¿Quién está viendo?" y ahí elegís con los números.
- **Verde** va al inicio, busca la fila "Seguir viendo" y abre el primer título.
  - Si aparece "¿Quién está viendo?", te pide que elijas un perfil, o elige solo el de `default_profile` si lo configuraste.
- Para que un solo botón haga todo, usá una macro (ver abajo).

### Mantener apretado

Las flechas, los ±10 s y el volumen se repiten mientras mantenés el botón, si tu TV repite la tecla. Las macros, los perfiles y el botón inteligente se ejecutan una sola vez por pulsación. Para distinguir "mantener" de "apretar dos veces" se usa la liberación de la tecla (CEC 0x45); si tu TV no la manda, se usa `debounce_ms`.

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
| `browser.launch` | `"on_key"` | Chromium cerrado: `on_key` (OK lo abre), `always` (siempre abierto), `never` |
| `browser.user_data_dir` | `~/.config/chromium-netflix` | Perfil de Chromium propio |
| `browser.extra_args` | `["--password-store=basic"]` | Flags extra (por ejemplo tu `--user-agent`) |
| `browser.kiosk` | `true` | Pantalla completa sin barras |
| `browser.reduce_effects` | `true` | Sin animaciones, transiciones ni scroll suave (ver [Rendimiento](#rendimiento)) |
| `browser.stop_previews` | `true` | Pausar y ocultar los avances que Netflix reproduce solos en el inicio |
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
| `play_pause` `play` `pause` | Reproducción |
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
| `launch` | Abrir Chromium en Netflix |
| `reload` | Recargar la página |
| `macro:nombre` | Ejecutar una macro de `[macros]` |
| `none` o `""` | Nada (desactiva la tecla) |

Una **macro** es una lista de acciones. Si apretás cualquier tecla mientras una macro está corriendo, la macro se cancela.

## Comandos

```bash
netflix-cec aprender [--todo] [--crudo]   # ver las teclas CEC (--todo: todo el tráfico; --crudo: salida de cec-client)
netflix-cec diagnostico [--detalle]       # revisar CEC, Chromium y la página actual de Netflix
netflix-cec navegador [URL]               # abrir Chromium listo para el control (o traerlo al frente)
netflix-cec accion ACCIÓN [ACCIÓN…]       # ejecutar acciones ya mismo, p. ej.: netflix-cec accion smart_skip
netflix-cec servicio [--teclado]          # el servicio (lo usa systemd); --teclado = probar escribiendo teclas
netflix-cec --debug …                     # cualquiera de los anteriores con logs detallados
```

`netflix-cec accion` sirve para probar desde otra máquina por SSH, por ejemplo `netflix-cec accion nav:down ok`.

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
| Se cierra o se cae Chromium | Vuelve al contexto `offline`, reintenta conectarse cada 2 s y lo reabre con OK, o solo si `launch = "always"` (máximo una vez cada 15 s; tras 5 fallos seguidos espera 5 min) |
| Se recarga la página o Netflix navega | Vuelve a inyectar el script |
| El propio servicio falla | systemd lo reinicia (`Restart=always`) |

Reiniciar o detener el servicio **no cierra Chromium**, porque Chromium se abre en su propio scope de systemd.

## Rendimiento

En una Raspberry Pi, Netflix en Chromium puede andar pesado. netflix-cec trae activadas dos opciones que lo alivianan:

- **`reduce_effects = true`**:
  - Abre Chromium con `--force-prefers-reduced-motion` y `--disable-smooth-scrolling`.
  - Inyecta CSS que deja todas las transiciones y animaciones de la página en 1 ms y desactiva el desenfoque de fondo (`backdrop-filter`), que es caro para la GPU de la Pi. Las animaciones quedan en 1 ms y no en "nada" para que el código de Netflix que espera el final de una animación (por ejemplo, el paso de página de las filas) siga funcionando.
  - El resaltado y el scroll pasan a ser instantáneos.
- **`stop_previews = true`**: pausa y oculta los videos que Netflix reproduce solos mientras navegás (el avance del inicio y las vistas previas). No afecta al reproductor.

Para volver a los efectos normales, en tu `config.toml`:

```toml
[browser]
reduce_effects = false
stop_previews = false
```

Los flags se aplican al abrir Chromium y el CSS al reiniciar el servicio, así que después del cambio: `systemctl --user restart netflix-cec`, cerrá Chromium y volvé a abrirlo (OK en el control).

Otras cosas que ayudan bastante:

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

**Netflix no reproduce en el perfil nuevo** (error de DRM o de navegador no compatible): agregá en `browser.extra_args` los mismos flags que usás en tu Chromium habitual, por ejemplo tu `--user-agent`.

**Algo de Netflix dejó de funcionar** (el botón inteligente no encuentra nada, no detecta los perfiles…): Netflix cambia su web cada tanto.
1. Con esa pantalla abierta, corré `netflix-cec diagnostico --detalle`. Lista los atributos `data-uia` visibles y los métodos del reproductor.
2. Ajustá el selector correspondiente en `[selectors]` de tu `config.toml`. La lista completa está en `netflix_cec/config.py` (`DEFAULT_SELECTORS`).

**El resaltado se pierde o salta raro**: corré `netflix-cec --debug accion nav:down` y `netflix-cec diagnostico --detalle` para ver qué elementos considera navegables. Lo que no deba enfocarse se agrega a `selectors.ignore`.

## Seguridad

El puerto de DevTools escucha **solo en 127.0.0.1**. Cualquier programa que corra en la Pi con tu usuario podría controlar ese Chromium, igual que podría leer tus archivos. No abras ese puerto a la red (no uses `--remote-debugging-address=0.0.0.0`). El perfil `chromium-netflix` es solo para Netflix.

## Probar sin TV ni Netflix

```bash
python3 tests/e2e_mock.py                    # Chromium headless + Netflix simulado + teclas CEC simuladas (76 verificaciones)
python3 tests/e2e_mock.py --layout nuevo     # solo el diseño actual de Netflix (o --layout viejo)
python3 tests/serve_mock.py                  # sirve el Netflix simulado en http://www.netflix.com.localhost:8765/browse (?layout=viejo para el diseño anterior)
netflix-cec servicio --teclado               # escribir teclas por consola: u d l r ok b rojo verde amarillo azul 1..9
```

`tests/fake_cec_client.py` imita a `cec-client`: manda las teclas de `FAKE_KEYS` y puede "caerse" con `FAKE_CRASH_AFTER`. Sirve para probar la recuperación.

## Desinstalar

```bash
./uninstall.sh
```

La configuración y el perfil `chromium-netflix` se conservan.

## Créditos

- Los códigos de tecla CEC y la forma de leer libcec (tipo *recording device*, descartar las liberaciones como pulsaciones) salen de [cec-mini-kb](https://github.com/spleen1981/cec-mini-kb).
- CEC: [libcec](https://github.com/Pulse-Eight/libcec) de Pulse-Eight.
