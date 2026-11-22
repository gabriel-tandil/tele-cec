# tele-cec

> **Una aclaración:** no me enorgullezco de este proyecto. El código no es mío, es de Claude; yo solo lo fui dirigiendo y haciendo las pruebas y correcciones. Es hijo de la necesidad: lo empecé cuando Netflix dejó de dar soporte a su reproductor en la Xbox 360. Lo comparto con la esperanza de que a alguien le sea de utilidad.
>
> **Y la mía (Claude):** el código lo escribí yo, es cierto, pero las ideas, las decisiones sobre cómo tenía que responder cada tecla del control y muchos de los errores corregidos salieron de quien lo usa todas las noches frente a la TV. Sin eso, habría sido un programa que pasa los tests; con eso, es uno que se usa.

Maneja **Netflix**, **YouTube**, **Tidal**, **videos** y **música** locales (mpv), **radios por internet** (mpv), **fotos** (mpv) y la **pantalla de un celular Android** (scrcpy) con el **control remoto de la TV** (HDMI-CEC), pensado para una Raspberry Pi 4 / Pi 400 con Raspberry Pi OS (Bookworm o Trixie) conectada a la TV. Netflix y YouTube se ven en **Firefox** (o en Chromium, como alternativa).

No convierte el control en un teclado, como hace [cec-mini-kb](https://github.com/spleen1981/cec-mini-kb): entiende qué hay en pantalla y hace lo que corresponde.

- **Navegación espacial** con las flechas por perfiles, filas de títulos, fichas y menús, con un **resaltado bien visible** del elemento enfocado. OK = clic, Atrás = volver.
- **Menú de actividades**: el botón **Menú** abre un menú en el centro de la pantalla, encima de lo que esté abierto, para elegir **Netflix, YouTube, Tidal, Videos y música, Radio, Fotos, Juegos (MAME), Android** u otros programas. Elegir una cierra la que estaba abierta. Ver [Menú de actividades](#menú-de-actividades).
- **YouTube para TV**: la versión de YouTube de los Smart TV, hecha para manejarse con flechas, OK y Atrás (ver [YouTube](#youtube)).
- **Navegador**: cualquier página web con el control remoto: las flechas van al enlace más cercano, OK lo abre, los números escriben como en un celular viejo y CC prende un modo mouse (ver [Navegador](#navegador)).
- **Control desde el celular**: una página en `http://raspberrypi.local:8765/` con el mismo menú de actividades de la TV, buscar en Netflix y YouTube, tu Tidal y tu música local (biblioteca, búsqueda y cola), lo que está sonando y la salida de sonido (ver [Control desde el celular](#control-desde-el-celular)).
- **Android**: la pantalla del celular en la TV, por USB o Wi‑Fi, con su sonido, y manejable con el control (ver [Android](#android)).
- **Videos, música y fotos** de un pendrive o de una carpeta local, en pantalla completa y con el control: mpv para los videos y la música (con una barra para la TV que, al buscar, muestra una miniatura del punto elegido) y una presentación con mpv para las fotos.
- **Botón inteligente** (🟡 amarillo en el reproductor): salta la intro o el resumen, o pasa al siguiente episodio, según lo que muestre Netflix.
- **Perfiles y "Seguir viendo"**: elegís el perfil con los números y retomás lo último visto con el rojo (en el inicio).
- **Tidal** con [High Tide](#tidal): play/pausa, temas, volumen, "me gusta" y "Mis canciones" con el control.
- **Reproducción**: play/pausa, ±10 s, volumen, y ciclar subtítulos y audio, con avisos en pantalla.
- **Modo aprendizaje**: muestra en consola el código y el nombre de cada tecla CEC que llega.
- **Servicio de usuario de systemd** con logs en journald. Se recupera solo si se cierra el navegador o se cae el adaptador CEC.

## Cómo funciona

```
Control remoto ──IR──▶ TV ──HDMI-CEC──▶ cec-client (libcec) ──▶ tele-cec ──DevTools o WebDriver BiDi (127.0.0.1:9222)──▶ Firefox o Chromium + Netflix
```

1. **CEC**: `tele-cec` ejecuta `cec-client` (paquete `cec-utils`, la misma libcec que usan Kodi y cec-mini-kb) y lee el tráfico CEC. La TV manda `USER_CONTROL_PRESSED` (0x44) con el código de la tecla, que es el mismo de `map.txt` de cec-mini-kb (0 = OK, 1–4 = flechas, 13 = Exit, 113–116 = colores, 32–41 = números…). Si `cec-client` se cae, se reinicia solo.
2. **Contexto**: antes de cada tecla, el servicio le pregunta a la página dónde está: perfiles, inicio/fichas, reproduciendo, un menú sobre el reproductor, un campo de texto, o el navegador cerrado. La misma tecla hace cosas distintas según el contexto: por ejemplo, la flecha derecha navega en el inicio y adelanta 10 s en el reproductor.
3. **Navegador**: Firefox (o Chromium, ver [Firefox y Chromium](#firefox-y-chromium)) se abre con el puerto de control solo en `127.0.0.1`: DevTools en Chromium, WebDriver BiDi en Firefox. Por ahí el servicio:
   - inyecta un script con la navegación espacial, el resaltado y los avisos;
   - hace clics "reales" con el mouse, que Netflix acepta igual que los de una persona;
   - controla el reproductor con la API interna de Netflix. Si no está disponible, usa los atajos de teclado de Netflix.

## Requisitos

- Raspberry Pi OS Bookworm (o Debian/Ubuntu) con escritorio, Wayland o X11.
- Firefox con Netflix funcionando, o Chromium con Netflix funcionando (`libwidevinecdm0`; ver [Firefox y Chromium](#firefox-y-chromium)).
- `/dev/cec0` (lo crea el driver `vc4-kms-v3d`, que viene activo por defecto).
- Python ≥ 3.11 (Bookworm trae 3.11). Todo lo demás (`cec-utils`, el navegador, mpv, feh, High Tide, scrcpy…) lo instala el instalador si falta.

## Instalación

```bash
# copiá esta carpeta a la Pi (git clone, scp, pendrive…) y dentro de ella:
./install.sh --copiar-perfil
```

| Opción | Qué hace |
|---|---|
| `--chromium` | Usa Chromium en vez de Firefox (que es el de por defecto): deja `engine = "chromium"` en `[browser]` de tu configuración. |
| `--sin-videos` · `--sin-fotos` | No instala mpv (Videos y Música) o feh (Fotos). |
| `--sin-tidal` | No instala High Tide (Tidal). Sin esta opción instala High Tide (Flatpak, para tu usuario) y la accesibilidad (AT-SPI), y configura el portal de secretos (ver [Tidal](#tidal)). |
| `--sin-android` | No instala scrcpy ni adb (Android). Sin esta opción instala `adb`, `avahi-utils` y `qrencode`, y compila scrcpy, que no está en los repositorios (unos minutos en una Pi; ver [Android](#android)). |
| `--copiar-perfil` | Copia tu perfil de Firefox al perfil propio de tele-cec para Netflix (`~/.config/firefox-netflix`), así no tenés que volver a iniciar sesión. **Cerrá Firefox antes.** Con `--chromium` copia tu perfil de Chromium (`~/.config/chromium`) a `~/.config/chromium-netflix` (funciona porque usás `--password-store=basic`). |
| `--apagado-automatico` | La Pi entra en reposo tras un rato sin sonido ni teclas y vuelve con el control o al prender la TV (ver [Reposo automático](#reposo-automático)): activa `[power]` e instala una regla de polkit (para `mode = "apagar"`). |
| `--sin-apt` | No instala paquetes, solo avisa si falta algo (las apps que falten no aparecen en el menú). |
| `--sin-servicio` | Instala todo pero no habilita el servicio. |

El instalador:
- instala lo que falte: los paquetes que necesita el servicio y las apps del menú (Firefox o Chromium, mpv y mpv-mpris, feh, High Tide, scrcpy); lo que ya está no lo toca, así que se puede volver a correr sin problema;
- copia el programa a `~/.local/share/tele-cec`;
- crea el comando `~/.local/bin/tele-cec`;
- crea la configuración `~/.config/tele-cec/config.toml` (si no existe) y el acceso **"Netflix (TV)"** en el menú;
- habilita el servicio `tele-cec` de systemd (usuario).

Si te agrega al grupo `video` para usar `/dev/cec0`, reiniciá la Pi.

> **¿Por qué un perfil de Chromium aparte?** Desde la versión 136, Chromium no permite el puerto de depuración (DevTools) sobre el perfil por defecto. Por eso tele-cec usa `~/.config/chromium-netflix`. Tu Chromium de siempre no cambia.

## Primer uso

1. Cerrá Kodi: dos programas no pueden usar CEC a la vez.
2. Mirá qué manda tu control:
   ```bash
   tele-cec aprender
   ```
   Apretá botones: vas a ver el nombre, el código y qué hace cada uno. Si el servicio estaba corriendo, el comando lo detiene mientras tanto y lo vuelve a iniciar al salir con Ctrl+C.
3. Abrí Netflix: apretá **Menú** en el control (o **OK** con todo cerrado), elegí **Netflix** y apretá OK (lo abre en pantalla completa). También podés usar **"Netflix (TV)"** en el menú del escritorio. La primera vez iniciá sesión, salvo que hayas usado `--copiar-perfil`.
   Si el menú no aparece con el botón Menú, mirá en el paso 2 qué código manda ese botón (ver [Menú de actividades](#menú-de-actividades)).
4. Verificá que todo esté bien:
   ```bash
   tele-cec diagnostico
   ```

## Teclas por defecto

Si una tecla no está definida en el contexto actual, se busca en el siguiente: `profiles → browse → global`, `player → global`, `player_menu → browse → global`, `tidal → global`, `videos → global`, `fotos → global`, `fotos_zoom → fotos → global`. Con el menú abierto (`menu`) solo valen las teclas del menú.

**Menú** (en cualquier contexto, también con todo cerrado) abre el [menú de actividades](#menú-de-actividades). Con todo cerrado, **OK** y **Atrás** también lo abren. **Atrás** vuelve un paso dentro de cada app y, cuando ya no hay a dónde volver (el inicio de Netflix arriba de todo, la primera pantalla de Tidal, la primera página del Navegador), cierra la app y muestra el menú; en Videos, Música, Radio y Fotos cierra de una, como ■. Los colores son de cada app: el 🟢 verde queda libre, porque muchas TVs lo usan para sus propias funciones.

**Netflix:**

| Tecla | Inicio / fichas (`browse`) | Reproduciendo (`player`) |
|---|---|---|
| Flechas | Moverse (con resaltado) | ◀ ▶ −/+10 s · ▲ ▼ volumen |
| OK | Clic (la 1.ª vez solo muestra el resaltado) | Play / pausa |
| Exit / Return | Volver · cerrar ficha · subir al principio · arriba de todo en el inicio: cerrar Netflix | Salir del reproductor |
| CC (subtítulos) | — | Ciclar subtítulos |
| 🔵 Azul | Ir al inicio | ¿Quién está viendo? (perfiles) |
| 🔴 Rojo | **Seguir viendo** (retomar lo último) | Ciclar audio |
| 🟡 Amarillo | **Mi lista** | Botón inteligente: saltar la intro o el resumen, o el siguiente episodio |
| ▶ Play · ⏸ Pausa | Reproducir el título resaltado o el de la ficha abierta | Play · pausa |
| ■ Stop | — | Salir del reproductor |
| ⏪ ⏩ | — | −/+1 minuto |
| Canal + | — | Siguiente episodio |
| 0 | ¿Quién está viendo? (perfiles) | Modo navegación (flechas para moverse por los controles) |
| 1 | ★ Agregar a **Mi lista** el título resaltado o el de la ficha abierta (otra vez lo saca) | — (todavía no: avisa que se hace desde la ficha) |

**1 = ★ Mi lista:** sobre una tarjeta abre su ficha, aprieta «Mi lista» y la cierra; con la ficha abierta aprieta su botón. Viendo un video todavía no (el reproductor no tiene ese botón). El botón se reconoce por su rótulo: si Netflix lo cambia, se ajusta con `[selectors] my_list`.

**Tidal** (ver [Tidal](#tidal)). Abre en **Inicio**, en pausa, con las flechas recorriendo la interfaz; **0** cambia las flechas a controles (y otra vez 0, de vuelta):

| Tecla | Recorriendo la interfaz (`tidal_nav`, así arranca) | Controles (`tidal`, después de 0) |
|---|---|---|
| Flechas | Mover el foco al elemento más cercano en esa dirección | ◀ ▶ −/+10 s · ▲ ▼ volumen |
| OK | Abrir / elegir (un disco, una playlist, un tema) | Play / pausa |
| Exit / Return | Atrás en Tidal (en la primera pantalla: cerrar Tidal) | Igual |
| Canal − · Canal + | Tema anterior / siguiente | Igual |
| ▶ · ⏸ · ■ | Play · pausa · pausa | Igual |
| ⏪ ⏩ | −/+1 minuto | Igual |
| 1 | ★ Me gusta (agregar o quitar de favoritos) | Igual |
| 🔵 Azul | Qué suena: tema, artista, disco y por dónde va | Igual |
| 🟡 Amarillo | En ciclo: Mis canciones → Colección → Inicio | Igual |
| CC (subtítulos) | Mostrar las letras (otra vez = volver al reproductor) | Igual |
| 🔴 Rojo | Mezclar / volver al orden | Igual |
| 2 · 3 · 4 | Inicio · Explorar · Colección | Igual |

GTK solo mueve el foco con flechas dentro de cada bloque (una fila de portadas, una lista), así que a muchos lugares no se llegaba. El parche que tele-cec le carga a High Tide (ver abajo) hace que las flechas vayan al elemento más cercano en esa dirección según dónde está en la pantalla, como en una TV, primero dentro del mismo panel (así, al final de una lista sigue bajando por la lista y no salta a la barra de abajo). Para que se vea qué está elegido, tele-cec le pone a High Tide un estilo con **marco y fondo amarillos** en el elemento con el foco (GTK lo dibuja muy tenue); el color se cambia con `[apps.tidal] focus_color` (`""` lo desactiva). Las **portadas** (discos, playlists, artistas) de High Tide no se pueden elegir con el teclado: solo se abren con un clic del mouse. Por eso, al abrirlo, tele-cec le carga un pequeño parche (un `usercustomize.py` en su carpeta de datos, sin modificar la app instalada) que hace que la portada reciba el foco, que **OK** abra las portadas y los atajos de Inicio (que High Tide solo abre con un clic), que las flechas se muevan por posición, y le abre un canal de control para que tele-cec cambie de pantalla (Inicio, Colección, Mis canciones, Atrás, Letras, me gusta) llamando a las mismas funciones que sus botones; se desactiva con `[apps.tidal] keyboard_cards = false` (entonces esas acciones vuelven a ir por accesibilidad).

**Videos** (`videos`) y **Música** (`musica`), con mpv, y **Fotos** (`fotos`), ver [Videos, música y fotos](#videos-música-y-fotos):

| Tecla | Videos · Música | Fotos |
|---|---|---|
| OK · ▶ · ⏸ | Play / pausa (si estás buscando, salta ahí) | OK: pausar o seguir la presentación · ▶ ⏸: con música de fondo, la música |
| ◀ ▶ | Buscar de a 10 s: la barra muestra a dónde (en videos, con una miniatura) y salta al soltar | Foto anterior / siguiente |
| ⏪ ⏩ | Buscar de a 1 minuto | — |
| ■ Stop | Cerrar y volver al menú (Videos se acuerda por dónde ibas) | Cerrar y volver al menú (la música de fondo sigue) |
| ▲ ▼ | Volumen | ▲ = zoom (ver abajo) · ▼ = cambiar de modo: solo fotos → fotos y videos → solo videos |
| Canal −/+ | Video o tema anterior / siguiente | Foto anterior / siguiente (también con zoom) |
| Atrás | Cerrar y volver al menú, como ■ (buscando con ◀ ▶: deja de buscar, sin saltar) | Cerrar y volver al menú (con zoom: alejar) |
| CC (subtítulos) | Videos: subtítulos | La fecha en una esquina (prender / apagar) |
| 🔵 Azul | Qué es: el nombre (en Música, tema, artista y disco), por dónde va y el número | Qué foto es (carpeta, archivo y número, en letra grande); **otra vez**, con el cartel a la vista: dónde se sacó (un mapa, si tiene GPS) |
| 🔴 Rojo | Videos: idioma de audio · Música: aleatorio (mezclar / volver al orden) | Girar la foto |
| 🟡 Amarillo | Videos: proporción (16:9, 4:3…) · Música: repetir (toda la lista → este tema → no repetir) | Una foto al azar |
| 1 | — | ★ Guardar la foto en **Favoritas** (otra vez la saca) |

En Música, la barra dice abajo a la derecha si está en **aleatorio** y si **repite la lista** o **el tema**. Es lo mismo que muestran los botones del reproductor del celular: se puede cambiar desde cualquiera de los dos.

**Zoom en las fotos** (`fotos_zoom`): ▲ acerca la foto y pausa la presentación. Mientras hay zoom:

| Tecla | Fotos con zoom |
|---|---|
| ◀ ▶ ▲ ▼ | Mover la foto (ver otra parte) |
| OK · Canal + | Acercar más |
| Atrás · Canal − | Alejar; desde el primer paso vuelve a la foto entera |
| ▶ · ⏸ | Foto entera de una |

Al salir del zoom (o al pasar a otra foto con Canal ±) la presentación sigue sola, salvo que la hubieras pausado vos antes.

**Fotos y videos**: la presentación tiene tres modos.
- **Solo fotos** (el de siempre).
- **Fotos y videos**: las fotos el mismo tiempo de siempre y los videos de la carpeta en su lugar, ordenados por la fecha que traen los nombres (`IMG_20201130…`, `VID_20201108…`).
- **Dónde se sacó** (azul dos veces): en las fotos que tienen la ubicación (GPS, como las del celular), el azul muestra qué foto es y avisa «Azul otra vez: dónde se sacó»; apretándolo otra vez mientras se ve ese cartel aparece un **mapa** con una marca en el lugar, la fecha y la carpeta; ▲ u OK acercan, ▼ aleja y Atrás vuelve a la presentación (que se pausa mientras tanto). Los mosaicos son de [OpenStreetMap](https://www.openstreetmap.org/copyright): la Pi los pide a tile.openstreetmap.org (es lo único que sale de la casa: la zona del mapa, no la foto) y los guarda en `~/.cache/tele-cec/mapas` un mes. Necesita `python3-pil` (lo instala `install.sh`). `[apps.fotos] map = false` lo apaga; `map_tiles` cambia de dónde salen los mosaicos.
- **Un día como hoy**: en la carpeta de Fotos (Carpetas ▸, o tocando Fotos › Carpetas en el celular), después de los tres modos, «▶ Un día como hoy» muestra las fotos y videos de esta fecha en otros años, al azar («15 de noviembre: 312 fotos y 4 videos de 2009, 2014 y 2017»), con el año de cada una chico abajo a la derecha («2014 · hace 12 años»). Si no hay de ese día exacto, los de esa semana. La fecha sale del nombre (IMG_20170226_…, IMG-20180321-WA…, una carpeta «20-12-2008 …») o, para las de cámara (DSC02815.JPG), de la foto misma (EXIF). Leer todas la primera vez lleva un rato: va de a poco (unas 4 fotos por segundo, para no trabar la red de la Pi) y se pausa mientras se están mirando las fotos; mientras tanto el menú dice «buscando fechas (…%)»; quedan guardadas en `~/.cache/tele-cec/fotos-fechas.json` y las nuevas se leen al actualizarse la lista. Primero se guardan las que dice el nombre (al instante) y después las del EXIF; si el servicio se reinicia, a los 2 minutos sigue solo desde donde quedó. Las que se leen al mirar una foto (la fecha en la esquina) también quedan guardadas. ▼ pasa a la carpeta entera. `on_this_day = false` lo oculta; `today_shuffle = false` las muestra de la más vieja a la más nueva.
- **La fecha en la esquina** (**CC** en el control): prende o apaga la fecha de cada foto, chica abajo a la derecha, con lo que se sepa: el día («sábado 20 de diciembre de 2008 · hace 17 años») si lo dice el nombre o la foto (EXIF); si no, el mes o el año según las carpetas («2003/Diciembre», «2009/11», «2008-12 Navidad», «Vacaciones enero 2010» → «diciembre de 2003», «noviembre de 2009»…; «Fotos 1998» → «1998»). Sin nada, la esquina queda vacía. Si la foto o el video sabe **dónde se sacó** (GPS: EXIF de las fotos, o la ubicación que guardan los celulares en los videos MP4/MOV), a la derecha de la fecha aparece un **globo terráqueo**: el azul dos veces muestra el mapa (también de los videos). `show_location = false` lo saca. Queda como la dejaste para la próxima vez (`show_date` es cómo arranca la primera vez). En «Un día como hoy», con la fecha prendida se ve el día entero en vez de solo el año. Solo con mpv (no con `viewer = "feh"`).
- **Favoritas** (**1** en el control, como el ★ de Tidal): guarda la foto (o el video) que se está viendo en una lista; otra vez 1 la saca, y en la esquina de abajo a la derecha se ve un **★** en las guardadas (con la fecha, si está prendida). Para verlas: en **Carpetas ▸**, al final de la lista de carpetas (y dentro de cada una, después de «Un día como hoy»), «▶ ★ Favoritas» las muestra en el orden en que se guardaron («12 fotos y 1 video guardadas con 1»); lo mismo en la página del celular, que además ofrece «★ Guardar esta foto» tocando Fotos cuando está en la TV. La lista está en `~/.local/share/tele-cec/fotos-favoritas.txt` (una ruta por línea: se puede editar o copiar), fuera de la caché para que no se pierda. También con feh.
- **Al azar**: si la carpeta tiene subcarpetas con fotos o videos (los años, los viajes…), la presentación va en orden aleatorio, en los tres modos; si no, en orden (por nombre, o intercalados por fecha). Con `shuffle = true`, siempre al azar. El menú lo dice debajo de cada opción («· al azar»). Al cambiar de modo con ▼ sigue desde la foto que se estaba viendo.
- **Solo videos**: los videos de esa carpeta y sus subcarpetas.

Los tres usan **mpv** (`sudo apt install mpv`). Al entrar a la presentación se ve unos segundos en qué modo arranca, y **▼** pasa al siguiente sin perder el lugar: sigue desde la foto que se estaba viendo (o el video más cercano por fecha). También se puede elegir en **Carpetas ▸**: dentro de una carpeta, debajo de «Ver las fotos» están «Fotos y videos» y «Solo los videos», y lo mismo en la página del celular. OK sobre Fotos siempre arranca en «solo fotos». Mientras suena un video, la música de Tidal de fondo se pausa y vuelve al terminar el video; ▶ y ⏸ pausan el video.

Al abrirse una ficha, el resaltado pasa solo a su botón **Reproducir/Reanudar** (OK dos veces = abrir y reproducir), y al cerrarla vuelve a la tarjeta.

Otros contextos:
- **"¿Quién está viendo?"** (`profiles`): los números **1–5** eligen el perfil; también funcionan las flechas + OK.
- **Menú sobre el reproductor** (`player_menu`): se detecta solo (selector de audio/subtítulos, episodios, pantalla final…) y las flechas navegan. Con **0** se fuerza este modo.
- **Campo de texto** (`text`, por ejemplo un PIN): los números escriben dígitos.
- **Nada abierto** (`offline`): **OK** o **Menú** abren el menú de actividades.

> Las teclas de **volumen de la TV** casi nunca viajan por CEC: controlan la TV directamente, como siempre. El "volumen" de la tabla es el del reproductor de Netflix.

### Botón inteligente

Busca en pantalla, en orden: **Saltar intro**, **Saltar resumen**, **Saltar** (avance) y **Siguiente episodio**, y aprieta el primero que encuentra. Si no hay nada, avisa "Nada para saltar". Si lo apretás otra vez dentro de 4 s, pasa al **siguiente episodio**.

### Perfiles y "Seguir viendo"

- **0** (en el inicio) abre "¿Quién está viendo?" y ahí elegís con los números.
- **Rojo** (en el inicio o en una ficha) va al inicio, busca la fila "Seguir viendo" y abre el primer título.
  - La fila se reconoce por su tipo, por su título («Continuar viendo contenido de…», «¿Seguimos, …?» en los perfiles infantiles) o, si en ese perfil se llama de otra forma, por ser la primera fila con tarjetas a medio ver (las de la barrita de progreso). Si no aparece, baja de a poco hasta encontrarla.
  - El título se abre directo por su número (`/watch/…`), sin abrir su ficha: en una serie, Netflix sigue en el episodio donde quedó.
  - Si aparece "¿Quién está viendo?", te pide que elijas un perfil, o elige solo el de `default_profile` si lo configuraste.
  - Si no la encuentra, el registro (`journalctl --user-unit tele-cec`) dice qué filas había, cuántas tarjetas tenía cada una y cuántas a medio ver.
- Para que un solo botón haga todo, usá una macro (ver abajo).

### Avisos mientras trabaja

- **Indicador "trabajando"**: si una acción tarda más de un cuarto de segundo (▶ en la lista, rojo, perfiles, inicio…), arriba a la derecha aparece un circulito con lo que está haciendo: «Abriendo «Merlina»…» → «Cargando «Merlina»…». No se apaga al cambiar de página: sigue hasta que arranca el video o termina de cargar la pantalla (como máximo 15–20 s). **Atrás** lo cancela.
- **Al abrir Netflix**: el panel del menú muestra «Abriendo Netflix…» mientras arranca el navegador, que abre en una página negra (sin destello blanco). Después sigue el indicador hasta que Netflix termina de cargar.
- **Eco de teclas** (apagado): con `feedback.key_echo = true`, cada tecla que llega se muestra un instante arriba a la izquierda. Sirve para saber si la TV no mandó la tecla o si llegó y la acción no hizo nada.
- **Aviso en la TV** (apagado): con `feedback.cec_osd = true`, al abrir el navegador se le pide a la TV por CEC que muestre "Netflix...". Muchas TVs lo ignoran: probalo con la tuya.
- En el log, las acciones que tardan un segundo o más quedan registradas con su duración (`tele: continue_watching: 2.3 s`).

### Mantener apretado

Las flechas, los ±10 s y el volumen se repiten mientras mantenés el botón, si tu TV repite la tecla. Las macros, los perfiles y el botón inteligente se ejecutan una sola vez por pulsación. Para distinguir "mantener" de "apretar dos veces" se usa la liberación de la tecla (CEC 0x45); si tu TV no la manda, se usa `debounce_ms`.

## Menú de actividades

El botón **Menú** del control abre un menú en el centro de la pantalla para elegir qué usar: **Netflix**, **YouTube**, **Tidal**, **Videos y música**, **Radio**, **Fotos**, **Navegador**, **Juegos**, **Android** y los programas que agregues. **Videos y música** es una fila que abre otra lista con **Videos** y **Música** (Atrás vuelve al menú); cada una tiene su botoncito **Carpetas**. Aparece encima de lo que esté abierto, aunque esté en pantalla completa.

Es la pantalla de inicio: **aparece sola al prender la Pi** (`[menu] open_on_start`) y al volver del reposo. Sin nada abierto no se cierra sola; con una app abierta se cierra a los 10 s sin teclas (`timeout_s`).

Las opciones van en **dos columnas**, sin desplazar (`[menu] columns = 2`; con `1`, una sola lista como antes).

| Tecla | Qué hace |
|---|---|
| ▲ ▼ | Moverse dentro de la columna |
| ◀ ▶ | Pasar a la otra columna |
| OK | Abrir la actividad resaltada, de una |
| ▶ (sobre Videos, Fotos, Radio o Android) | Enfoca el botoncito de la fila, **Carpetas**, **Radios** o **Conectar**: OK lo abre (elegir otra carpeta, otra radio, conectar o vincular el celular). Otro ▶ pasa a la otra columna, ◀ vuelve a la fila |
| Amarillo | Las opciones de la fila, como el botoncito (también en Juegos y Sonido) |
| Menú (otra vez) | Lo mismo que Atrás: vuelve de una lista de adentro (Carpetas, Radios, Sonido, Ayuda…) o cierra el menú |
| «Sonido» | El volumen (con barra) y por dónde sale el sonido: TV (HDMI), Bluetooth… (ver abajo) |
| «Ayuda» (última opción) · Ayuda · el punto (•) | Qué hace cada tecla en cada app (ver abajo) |
| 1–9 | Elegir esa opción directo |
| Exit / Return | Cerrar el menú sin cambiar nada |

- **Con el teclado** (el de la Pi 400 o uno USB/Bluetooth), mientras el menú está a la vista: flechas, **Enter** (o espacio) para elegir, **Esc** o **Retroceso** para volver, números, **F1** para la ayuda y la tecla Menú para volver o cerrar. El panel toma el teclado solo mientras se ve el menú; al cerrarse, las teclas vuelven a la app (`[menu] keyboard = false` lo desactiva).
- Al abrirse, queda resaltada la actividad **en uso**. OK sobre ella solo cierra el menú.
- **Ayuda**: la última opción del menú (o, desde cualquier app, la tecla del punto (•), como recuerda abajo el menú, o Ayuda si tu TV la manda: muchas no la pasan por CEC, ni Info) muestra una tabla «tecla → qué hace», empezando por la app que estabas usando. ◀ ▶ pasan a otra app (Netflix en el inicio y viendo un título, Tidal, Videos, Fotos, Fotos con zoom, Android, el propio menú…), ▲ ▼ muestran más filas y Atrás vuelve. Se arma con tu configuración de teclas, así que si cambiás alguna en `config.toml` la ayuda lo refleja.
- **Música mientras pasan las fotos**: abrir Fotos no cierra Tidal ni la Música local (con la Música pasa lo mismo que se cuenta acá de Tidal), así que podés poner música y después la presentación. En el menú, Tidal figura «sonando» (o «en pausa», o «abierto» si no tiene nada cargado). Mientras tanto, ▶ y ⏸ ponen play o pausa a la música (con un aviso de qué suena); OK pausa la presentación y ◀ ▶ o Canal ± pasan de foto. Para cambiar de tema, usá la página del celular o elegí Tidal en el menú: cierra las fotos y deja Tidal en pantalla, sin cortar la música. Abrir Netflix, Videos u otra app sí cierra la música. Sin música, ▶ y ⏸ pausan la presentación.
- **Videos y Música: OK no arranca nada**: abre el selector en la carpeta predeterminada (el pendrive, o `~/Vídeos` / `~/Música`) para elegir qué poner. En **Videos** se elige el archivo: aparecen las subcarpetas y después los videos, y OK sobre uno reproduce solo ese; al terminar vuelve el menú. En **Música** la primera opción es «▶ Escuchar esta carpeta» (dice qué tiene: «12 temas», «12 temas y 3 subcarpetas», «en 3 subcarpetas»); abajo, las subcarpetas (artistas, discos) y los temas de esa carpeta: OK sobre un tema lo pone y sigue con los demás de la carpeta, como un disco.
  - **Lo que dicen los archivos**: cada tema muestra el título de sus etiquetas y, abajo, el artista (el disco, si no es el de la carpeta) y la duración; cada video, su título (si lo trae; si no, el nombre del archivo), la duración, la calidad (1080p, 4K…), el tamaño y si tiene subtítulos (adentro o al lado). Los videos se leen con `ffprobe` (paquete `ffmpeg`, lo instala `install.sh`) y lo leído se guarda en `~/.cache/tele-cec/videos-info.json`: la primera vez, si una carpeta tiene muchos videos, algunos pueden aparecer solo con el tamaño (se terminan de leer de fondo y la próxima vez ya salen completos). ◀ sube a la lista de carpetas configuradas (si hay más de una) y de ahí al menú. Si Videos o Música ya están abiertos, OK vuelve a ellos como siempre.
- **Elegir otra carpeta**: OK sobre Fotos arranca la presentación en la carpeta predeterminada (la primera de `[apps.fotos] folders`). Con **▶** (o el botoncito «Carpetas») se abre un selector de carpetas en el mismo panel:
  - primero las carpetas configuradas (`folders`), con la predeterminada marcada;
  - **OK** o **▶** entra en una carpeta y muestra sus subcarpetas (por ejemplo, los años de las fotos), y la primera opción es «▶ Escuchar esta carpeta» o «▶ Ver las fotos de esta carpeta», con todas sus subcarpetas (en Videos, en cambio, los videos de la carpeta, para elegir uno; en Música, debajo, también los temas);
  - **◀** o **Atrás** sube un nivel; desde arriba de todo vuelve a la lista de actividades;
  - si en una carpeta no hay música o fotos, esa opción aparece en gris (en Videos, «No hay videos en esta carpeta»);
  - la carpeta elegida vale solo esa vez: la próxima vez vuelve la predeterminada. Si Videos o Fotos ya estaban abiertos, se reabren en la carpeta nueva.
- **Temporizador para apagar**: al cumplirse baja el volumen de a poco (15 s), pausa todo, deja el volumen como estaba y la Pi entra en [reposo](#reposo-automático), que también apaga la TV. Con «al terminar este tema o video» pausa apenas termina, sin bajar el volumen. El último minuto la TV muestra una cuenta regresiva: **cualquier tecla lo retrasa 15 minutos** (`[power] timer_postpone_minutes`), también mientras baja el volumen, que vuelve a como estaba; esa tecla no le llega a la app. «Sonido» en el menú dice cuánto falta («se apaga en 28 min»). También se elige desde el celular. **Desde cualquier pantalla: el punto (•) dos veces seguidas** muestra cómo está («Temporizador · Se apaga en 28 min · • otra vez: en 45 minutos») y, mientras se ve ese aviso, cada toque del punto pasa a la opción siguiente (15 → 30 → 45 → 60 → 90 min → al terminar el tema, si suena algo → **apagado**), como el botón Sleep de las teles; «Temporizador apagado» termina el recorrido (el aviso se va y el próximo toque vuelve a ser la Ayuda); la app de abajo no recibe esas teclas. Un toque solo sigue siendo la Ayuda (el segundo la cierra). `[power] timer_key` cambia la tecla (`""` lo apaga) y `timer_double_press_ms` (1000) cuánto puede pasar entre los dos toques.
- **Sonido**: la anteúltima opción muestra por dónde sale el sonido ahora y el volumen («Auriculares BT · volumen 40 %»). Con OK o ▶ se abre: arriba, la fila **Volumen** con su barra (◀ ▶ lo bajan y suben de a 5 %, OK pone o saca el silencio; subirlo o bajarlo también saca el silencio), después el **Temporizador para apagar** (◀ ▶ u OK pasan por 15, 30, 45 minutos, 1 hora, 1 hora y media, «al terminar este tema o video» y ninguno; ver abajo) y la lista de salidas: la TV por HDMI, los parlantes o auriculares **Bluetooth** emparejados (aunque estén desconectados: elegirlos los conecta), y las salidas analógicas o USB que haya. Mientras la lista está abierta se **buscan parlantes y auriculares Bluetooth cercanos** (25 s; los teléfonos, teclados y demás no aparecen): los que se encuentran se agregan al final como «nuevo», y elegir uno lo empareja, lo marca de confianza (así después se reconecta solo) y lo conecta. Para que aparezca, ponelo en modo emparejamiento; la última fila, «Buscar Bluetooth otra vez», repite la búsqueda. OK pasa el sonido ahí (lo que esté sonando se muda solo) y, sobre una salida, ◀ o Atrás vuelve. Elegir la TV (o cualquier salida que no sea Bluetooth) también **desconecta** el parlante o los auriculares Bluetooth: no quedan tomados y al reiniciar la Pi no vuelven a conectarse solos. Usa PipeWire (`wpctl`) y `bluetoothctl`. Se oculta con `[menu] sound = false`.
- Elegir otra **cierra la actual** (pausando antes lo que esté sonando) y abre la nueva. Mientras tanto el menú muestra «Abriendo…» y las teclas se ignoran, para que el cambio no quede por la mitad.
- Las actividades sin contenido (por ejemplo, Fotos sin pendrive ni fotos en `~/Imágenes`) aparecen en gris y avisan al elegirlas. Las que no están instaladas no aparecen.
- Si no tocás nada, el menú se cierra solo a los 10 s (`menu.timeout_s`).
- Con el menú abierto, ninguna tecla llega a la app de abajo.

**Qué código manda el botón Menú**: cada TV lo pasa distinto, y algunas no lo pasan (lo usan para su propio menú). Por defecto se aceptan `ROOT_MENU`, `CONTENTS_MENU` y `SETUP_MENU`. Mirá con `tele-cec aprender` qué manda el tuyo. Si no manda ninguno, usá otro botón:

```toml
[keys.global]
F3_GREEN = "menu"     # el verde abre el menú (dentro del menú, el verde vuelve atrás o lo cierra)
[keys.offline]
F3_GREEN = "menu"
```

**El panel**: es una ventana propia (GTK 3) que va en la capa superior del escritorio con **gtk-layer-shell**, la forma de Wayland (labwc) de poner algo encima de las apps en pantalla completa sin tomar el foco del teclado: el video de abajo sigue recibiendo sus teclas. El instalador agrega `gir1.2-gtklayershell-0.1`. Sin eso, el menú es una ventana común "siempre encima", que en algunos escritorios puede quedar detrás. Para ver cómo queda en tu TV: `tele-cec menu`.

```toml
[menu]
items = ["netflix", "tidal", "videos", "fotos"]   # orden; las de [activities.*] se agregan al final
timeout_s = 10
```

**Atajos**: `app:<actividad>` abre una actividad directo (si ya está abierta, hace su `on_reselect`). Sirve para poner una en un color, como antes:

```toml
[keys.global]
F4_YELLOW = "app:tidal"
[keys.offline]
F4_YELLOW = "app:tidal"
```

### Otras actividades

Cualquier programa se puede agregar al menú. Al elegir otra actividad se cierra con SIGTERM. Mientras está abierto, las teclas usan `[keys.app]`: solo Menú hace algo.

```toml
[activities.juegos]
name = "Juegos"
icon = "juegos.svg"              # incluidos: juegos, musica, peliculas (.svg); o un archivo, un ícono del tema o un emoji
command = ["mame", "-rompath", "~/mame/roms"]
process = "mame"                 # nombre del proceso, si no es el del comando
```

## Joystick

Un joystick (Xbox 360 o One, 8BitDo, PlayStation…) maneja todo igual que el control: el menú, Netflix, YouTube, Tidal, videos, fotos, Android, y también despierta a la Pi del reposo. Se detecta solo al conectarlo (USB, el receptor inalámbrico de Xbox 360 o Bluetooth). No hace falta instalar nada: alcanza con que tu usuario esté en el grupo `input` (en Raspberry Pi OS ya lo está). `tele-cec diagnostico` muestra los que están conectados.

| Joystick (Xbox) | Hace lo mismo que |
|---|---|
| Cruz · palanca izquierda | ▲ ▼ ◀ ▶ (mantenerla repite) |
| A | OK |
| B | Atrás |
| X | ⏯ |
| Y | Amarillo (en el Xbox los colores coinciden) |
| LB · RB | Canal − · Canal + (anterior · siguiente) |
| LT · RT (gatillos) | ⏪ · ⏩ |
| Start · botón del medio | Menú |
| Back | Azul |
| Apretar la palanca izquierda · derecha | Rojo · Verde |
| Palanca derecha ▲ ▼ | Vol + · Vol − |

La ayuda del menú tiene una página «Joystick» con lo que hace cada botón según tu configuración. Para cambiar un botón: `[gamepad.buttons]` en `config.toml` (por ejemplo `X = "F2_RED"`, o `X = ""` para que no haga nada).

En los **Juegos** y en los programas de `[activities.*]` (por ejemplo otro emulador) el joystick es del programa: ahí el servicio solo atiende el **botón del medio**, que abre nuestro menú (en los juegos, la lista con el juego en pausa). Ver `[gamepad] menu_only_in`, `menu_buttons` y `exit_buttons` (por ejemplo `exit_buttons = ["BACK:HOLD"]` para salir del juego manteniendo Back apretado 1 segundo). Firefox no lee el joystick (`dom.gamepad.enabled = false`), porque si no, en YouTube cada botón haría dos cosas.

## Reposo automático

Si no sale sonido y nadie toca el control durante un rato, la Raspberry Pi entra en **reposo** y apaga la TV por CEC. Una presentación de fotos sin música no cuenta como sonido: por eso el tiempo por defecto es largo.

- **Cuándo**: a las **3 horas** sin sonido ni teclas del control (`idle_minutes`), a los **15 minutos** si la TV avisó por CEC que se apagó (`tv_off_minutes`), o a los **10 minutos** si no hay ninguna actividad abierta, solo el menú en pantalla (`menu_idle_minutes`; la cuenta empieza cuando se cerró la última app).
- **Aviso**: antes, el panel muestra una cuenta regresiva de 60 s. **Cualquier tecla la cancela** (y la cuenta vuelve a empezar: otras 3 h, o los minutos que correspondan); esa tecla no le llega a la app, así un OK no pausa la película. También se cancela si vuelve a sonar algo.
- **En reposo**: pausa y cierra todo. La Pi sigue prendida escuchando CEC (gasta unos pocos watts).
- **Vuelve** cuando:
  - apretás **una tecla del control** (esa tecla solo despierta) — la TV cambia a la Pi;
  - **se prende la TV** (en reposo le pregunta cada minuto por CEC) — en este caso **no** le cambia la entrada, por si la prendiste para ver otra cosa. Si nadie toca el control, a los 10 minutos vuelve al reposo **sin apagar la TV**.
- **Varios días en reposo**: si pasan **72 horas seguidas** sin que nadie la despierte (`off_after_hours`), se apaga de verdad, sin tocar la TV. Apagada, el control y la TV ya no la despiertan: en una Pi 400 se prende con Fn+F10, o desenchufando y enchufando. Necesita la regla de polkit que instala `--apagado-automatico`; con `off_after_hours = 0` nunca se apaga.
- **Al volver aparece el menú de actividades**, sin abrir nada. Si preferís que restaure lo que había (`resume_apps = true`), lo reabre en pausa: Netflix en la misma página (si estabas viendo algo, en ese título, que Netflix retoma donde quedó) y con el mismo perfil; Tidal sin poner música; Videos y Música en la misma carpeta, archivo y minuto; Fotos en la misma carpeta (con la música de fondo, si estaba).

También se puede apagar en vez de reposar (`mode = "apagar"`), pero entonces el control no la vuelve a prender: CEC no funciona con la Pi apagada. En una Pi 400 se prende con Fn+F10 o desenchufando y enchufando, y hace falta una regla de polkit para que el servicio pueda apagar sin contraseña.

Se activa con `./install.sh --apagado-automatico` (pone `enabled = true` en tu configuración e instala la regla de polkit, que hace falta para apagar: `mode = "apagar"` u `off_after_hours`).

```toml
[power]
enabled = true
mode = "reposo"               # o "apagar"
idle_minutes = 180
tv_off_minutes = 15
menu_idle_minutes = 10        # sin ninguna actividad abierta, solo el menú (0 = esperar idle_minutes)
warn_seconds = 60
tv_standby = true             # apagar también la TV
wake_on_tv = true             # volver al prenderse la TV
off_after_hours = 72          # tras tantas horas seguidas en reposo, apagarla de verdad (0 = nunca)
resume_apps = false           # al volver: el menú (false) o lo que estaba abierto, en pausa (true)
```

`tele-cec diagnostico` muestra si está activo y si en este momento sale sonido. Para probar por SSH: `tele-cec tecla ok` manda una tecla como si viniera del control.

## Videos, música y fotos

- **Videos** abre **mpv** en pantalla completa con el video elegido en el menú (o en la página del celular) y se cierra solo al terminar. Con un `command` propio (VLC…) se sigue eligiendo la carpeta entera. Abajo aparece una **barra para la TV** al pausar, al empezar cada video y al buscar: con ◀ ▶ (o ⏪ ⏩) se mueve un cursor sobre la barra con el tiempo y una **miniatura** de ese punto, y cuando dejás de tocar (o con OK) salta ahí; Atrás deja de buscar. Las miniaturas las hace [thumbfast](https://github.com/po5/thumbfast) (MPL 2.0, viene incluido). Subtítulos que estén al lado del video se cargan solos; se prefiere el audio y los subtítulos en español.
- **Seguir viendo**: si cerrás un video a la mitad (Atrás, otra app, el reposo), mpv guarda dónde quedó, con el audio y los subtítulos elegidos (en `~/.cache/tele-cec/videos-seguir`). En la lista del menú y del celular ese video dice «quedó en 1:12:30», y al elegirlo pregunta: **▶ Seguir desde 1:12:30** o **Desde el principio**. Arriba de todo en cada carpeta de Videos aparecen los últimos de esa carpeta (o sus subcarpetas) que quedaron a la mitad («Seguir viendo: …»), que siguen directo; en la lista de carpetas aparecen todos, del más reciente al más viejo. Para sacar uno sin abrirlo: **rojo dos veces** sobre el video (en la tele) o la **✕** de la fila «Seguir viendo» (en el celular). Abajo del nombre de cada carpeta dice qué se está viendo adentro («viendo: La película (quedó en 1:06:40) y 2 más»). Si lo viste menos de un minuto, o lo cortaste en los créditos (los últimos 3 minutos), cuenta como no empezado o terminado. Si termina solo, se borra. `[apps.videos] resume = false` lo apaga.
- **Música** abre **mpv** con los temas de la carpeta (y subcarpetas, en orden natural: «2 - …» antes que «10 - …») y muestra la **tapa del disco**: la que trae el archivo o una imagen al lado (`cover.jpg`, `folder.jpg`…). La barra queda siempre a la vista con el tema, el artista y el tiempo. Puede quedar sonando mientras pasan las fotos.
- **En la Raspberry Pi**, los videos H.264 y HEVC se decodifican por hardware y se muestran con la salida `dmabuf-wayland` de mpv (1080p a 60 cuadros sin trabarse); las fotos, las tapas y los videos en otros formatos, con OpenGL. El cambio lo hace solo un script de mpv (`tele_cec/apps/mpv_scripts/tele_cec_vo.lua`) en cada archivo. Con Vulkan (lo que mpv elige solo) la tele se veía azul: las fotos salen en colores de 10 bits que el escritorio manda directo al HDMI, y los cuadros del decodificador por hardware Vulkan no los sabe leer. Las fotos de más de 4096 píxeles de lado (p. ej. 4160 × 3120, de 13 MP) se achican a 4096 antes de dibujarlas: la placa de video de la Pi no acepta imágenes más grandes (salían negras). Mientras se ve un video, el script deja un píxel casi transparente encima: si no, el escritorio mandaba el video directo a la placa de video y, cada vez que aparecía un subtítulo o la barra, pasaba a componerlo con la GPU, que convierte los colores distinto. En películas oscuras se veía un salto de brillo con cada subtítulo.
- Videos y Música se manejan por el canal de control de mpv; con **mpv-mpris** (lo instala `install.sh`) lo que suena aparece en la página del celular. Cada uno usa su propio mpv, sin la configuración personal de mpv (`~/.config/mpv`).
- **Radio**: OK sobre Radio en el menú pone **la última que sonó** de una (abajo de la fila dice cuál). El botoncito **Radios ▸** (▶, o el amarillo) muestra la lista de `[apps.radio] stations` (no viene ninguna: se ponen en `config.toml`), con la que suena o la última resaltada; OK (o 1 – 9) abre **mpv** con esa emisora, o la cambia si la Radio ya estaba sonando. La primera vez (todavía no sonó ninguna) OK también muestra la lista. En la TV, una pantalla propia: la emisora, «● En vivo» y el tema que está sonando, si la emisora lo manda («Artista - Tema»), con la **tapa del disco** a la izquierda si se encuentra: las emisoras mandan solo el texto, así que se busca en iTunes (público, sin cuenta; sale de la casa solo «Artista - Tema») y se acepta solo si el artista coincide, para no poner la tapa de otro; quedan guardadas en `~/.cache/tele-cec/radio-tapas` y la misma tapa se ve en el reproductor del celular. `covers = false` lo apaga. OK pausa y sigue (después de una pausa de más de 20 s vuelve a conectar, para escuchar lo que sale en vivo y no lo de hace un rato); ▲ ▼ el volumen; ◀ ▶ o Canal ± cambian de radio, 1 – 9 eligen una de la lista, el **amarillo** muestra la lista encima (Atrás vuelve a la Radio), el **azul** dice qué suena y ■ la cierra. Se acuerda de la última que sonó. Arranca con 3 s de colchón y, si se corta la conexión, vuelve a conectarse sola. Como la Música, puede quedar sonando mientras pasan las fotos (un video de la presentación la pausa) y, con un parlante Bluetooth, abrirla desde el celular no prende la TV.
- **Fotos** abre una presentación con **mpv** en pantalla completa: avanza sola cada 8 s y con ◀ ▶ pasás a mano. La placa de video achica las fotos, hace el zoom y las mueve (antes, con feh, todo lo hacía el procesador), y la foto siguiente se lee mientras se ve la actual: pasar de foto tarda 0,2 s aunque sean de 12 MP y estén en otra máquina. Las fotos sacadas con el celular girado salen derechas. OK la pausa. ▲ hace zoom y las flechas recorren la foto (ver [Teclas](#teclas-por-defecto)).
- **De dónde**: la primera carpeta de `folders` que tenga videos o fotos. Por defecto, primero los **pendrives** (`/media/<usuario>/<disco>`, donde los monta el escritorio) y después `~/Vídeos` o `~/Imágenes`. El menú muestra cuál va a usar («Pendrive KINGSTON», «~/Vídeos»).

```toml
[apps.videos]
folders = ["/media/*/*", "~/Vídeos", "~/Videos"]
shuffle = false
resume = true     # Seguir viendo: recordar dónde quedó cada video
sub_scale = 1.15  # tamaño de la letra de los subtítulos (1 = el de mpv; 1.3 = más grande)
command = []      # otro programa en vez de mpv; {carpeta} = la carpeta elegida, p. ej.:
                  # ["vlc", "--fullscreen", "{carpeta}"]

[apps.musica]
folders = ["/media/*/*", "~/Música", "~/Music"]
shuffle = false

[apps.radio]
stations = [   # nombre y dirección de la transmisión (mp3, aac, m3u, m3u8…); sin ninguna, Radio queda en gris
  { name = "Mi radio", url = "https://radio.example.com/stream.mp3" },
  { name = "Otra radio", url = "https://otra.example.com/live.m3u8" },
]   # las direcciones de casi todas las emisoras están en https://www.radio-browser.info

[apps.fotos]
folders = ["/media/*/*", "~/Imágenes", "~/Pictures"]
delay_s = 8
shuffle = false
today_shuffle = true  # «Un día como hoy» al azar
show_date = false     # la fecha en la esquina al principio (después, lo último elegido con CC)
show_location = true  # con la fecha: un globo terráqueo si la foto o el video tiene la ubicación (GPS)
scroll_step = 150 # píxeles que se mueve la foto con zoom por cada flecha
zoom_step = 25    # % de cada paso de zoom
viewer = "mpv"    # "feh" = el visor de antes para «solo fotos» (todo con el procesador)
command = []      # otro visor
```

**Muchas fotos o una carpeta de red.** Recorrer toda la carpeta antes de mostrar la primera foto tarda (con 50 000 fotos en otra máquina, 15-30 s). Por eso tele-cec arma la lista una vez, mientras el menú muestra «Abriendo Fotos…», la guarda en `~/.cache/tele-cec/` y las siguientes veces la presentación arranca al instante con ella. Después de abrir, la lista se actualiza en segundo plano (como mucho cada 10 minutos), así que las fotos nuevas aparecen la vez siguiente. En el menú, una carpeta de red figura como «En red: …».

Para una carpeta compartida por Samba (por ejemplo, desde otra Raspberry Pi), lo más cómodo es que se monte sola al usarla. En `/etc/fstab`, con el usuario y la contraseña en un archivo que solo lee root (`chmod 600`):

```
//servidor/Fotos  /mnt/fotos-red  cifs  credentials=/etc/samba/credenciales,ro,uid=1000,gid=1000,iocharset=utf8,nofail,_netdev,x-systemd.automount,x-systemd.idle-timeout=600  0  0
```

y en tu `config.toml`: `[apps.fotos] folders = ["/mnt/fotos-red", "/media/*/*", "~/Imágenes"]`. Con `nofail` y `x-systemd.automount`, la Pi arranca igual aunque la otra máquina esté apagada.

**Con feh** (`viewer = "feh"`): feh es una app X11 (corre por Xwayland) y sus atajos (pausa, zoom, girar) se le mandan con `xdotool`, que instala `install.sh`. Con `wtype` no sirve: Xwayland no usa su mapa de teclas y a feh le llega otra tecla (Escape, que lo cierra).

`tele-cec diagnostico` muestra qué carpeta usa cada actividad y cuántos archivos tiene.

## YouTube

YouTube se ve con **YouTube para TV** (`youtube.com/tv`), la versión que traen los Smart TV y las consolas: se maneja de fábrica con **flechas, OK y Atrás**, así que las teclas del control le llegan tal cual. **CC** prende o apaga los subtítulos: el servicio se lo pide al reproductor de la página por un canal de control propio (`[apps.youtube] debug_port`, 9224, solo en 127.0.0.1), porque la «c» en YouTube para TV abre la búsqueda.

- Se abre con el **mismo navegador que Netflix** (`[browser] engine`: Firefox por defecto, o Chromium), en una ventana aparte con su propio perfil (`~/.config/firefox-youtube` o `~/.config/chromium-youtube`) que se presenta como TV: YouTube muestra esta versión solo a TVs. No afecta al perfil de Netflix. Para usar otro navegador solo en YouTube: `[apps.youtube] engine = "chromium"` (o `"firefox"`).
- **Iniciar sesión**: «Sign in with your phone» muestra un código para entrar en `yt.be/activate` desde el celular (o un QR). También se puede mirar como invitado.
- **Rendimiento**: fuerza **H.264** (como la extensión h264ify), que la Pi decodifica por hardware; VP9 y AV1 irían por CPU. En Firefox lo hace con preferencias del perfil; en Chromium, con una extensión chiquita que trae tele-cec (`tele_cec/apps/youtube/h264`). En una Pi 400, un video en pantalla completa usa alrededor de medio núcleo.

| Tecla | Qué hace |
|---|---|
| ▲ ▼ ◀ ▶ · OK | Moverse y elegir (como en un Smart TV) |
| Atrás · ■ Stop | Volver (■ sale del video) |
| ▶ Play · ⏸ Pausa | Play · pausa |
| ⏩ ⏪ | Adelantar / atrasar 1 minuto |
| Canal + · Canal − | El video siguiente / anterior de la lista |
| 🔵 Azul | El inicio de YouTube |
| CC | Subtítulos (prender / apagar) |
| 1 | ★ **Me gusta** en el video que se está viendo (otra vez lo saca) |

**1 = ★ me gusta** (con un video puesto): si los controles están ocultos los muestra (como una flecha), aprieta el 👍 con un clic por el canal de control (al botón no le alcanza un clic por JavaScript) y avisa si quedó puesto o sacado. Hace falta haber iniciado sesión: como invitado, YouTube pide la cuenta.

```toml
[apps.youtube]
url = "https://www.youtube.com/tv"
engine = ""                   # "" = el de Netflix; o "firefox" / "chromium"
profile = ""                  # "" = ~/.config/firefox-youtube o ~/.config/chromium-youtube
force_h264 = true
```

## Navegador

**Navegador** abre cualquier página web en la TV y se maneja entera con el control remoto. Es un **Firefox aparte** (o Chromium: `[apps.navegador] engine`), en pantalla completa, con su propio perfil (`~/.config/firefox-navegador`), lanzado y controlado igual que el de Netflix pero por otro puerto (`debug_port = 9223`): tele-cec le inyecta un script en cada página (`tele_cec/apps/navegador/nav.js`) y con eso las flechas van al enlace o botón **más cercano en esa dirección**, con un marco blanco y azul, igual que en Netflix.

- **La página de inicio** es una propia, con los **sitios guardados** en cuadros chicos (sin la lista, vienen Google, DuckDuckGo y Wikipedia). Cada sitio se muestra con **su ícono** (favicon), también en la lista del menú y en el celular: se le pide al sitio mismo (el más grande que declare, o `/apple-touch-icon.png`, o `/favicon.ico`) y queda en `~/.cache/tele-cec/favicons`; si un sitio no tiene, va su inicial (o el globo, en el menú). El **amarillo** (o el botoncito **Sitios ▸** del menú) muestra la lista en el panel desde cualquier página, para abrir uno, y el **azul** vuelve al inicio. `home` cambia la página de inicio por cualquier dirección.
- **Guardar y sacar sitios**: el **1** es el ★, como en Netflix, YouTube, Tidal y Fotos. Con una página abierta, **1** la guarda al final de la lista (con el título de la página como nombre) y queda en la página de inicio; si ya estaba guardada, **1** la saca. En la lista (el amarillo, desde el Navegador o desde el menú), **1** sobre un sitio lo saca enseguida, sin preguntar (se vuelve a guardar con 1 en su página); el **rojo dos veces** también, preguntando antes. La lista también trae **★ Guardar esta página** (ya resaltado si la que se ve no está guardada), para guardar con OK. En el celular, el **★** del control remoto hace lo mismo que el 1. La primera vez que se guarda o se saca uno, la lista pasa a `~/.local/share/tele-cec/navegador-sitios.json` y desde ahí manda ese archivo (`bookmarks` de la configuración queda de lado; borrá el archivo para volver a ella).
- **Modo mouse** (**CC**): para lo que el marco no alcanza (el «No soy un robot» de un captcha, un video embebido o cualquier cosa adentro de un iframe, un menú que se abre al pasar por encima), las flechas mueven **una flecha dibujada** en la página: con un toque, de a poco (tocando seguido, cada vez más); **manteniendo apretada** la flecha del control, se desliza sola y acelera hasta que la soltás (si tu TV repite la tecla mantenida, o al menos avisa cuando la soltás; si no hace ninguna de las dos, tocá seguido) (al llegar al borde corre la página o la lista que tenga debajo). Se vuelve **mano** sobre lo que se puede tocar. **OK** hace un **clic de verdad** en ese punto (y la flecha se mueve de verdad: los menús «al pasar» se abren). Escribiendo en un campo con el modo mouse, los números escriben y OK busca, salvo que hayas movido la flecha después de la última letra: ahí OK es un clic. **CC** otra vez vuelve al marco, en lo más cercano a donde quedó la flecha. Sigue prendido al cambiar de página.
- **Escribir**: en un campo de texto (el buscador de Google, un formulario) **OK** entra a escribir y los **números escriben como en un celular viejo**: 2 = a b c, 3 = d e f, 6 = m n ñ o…, apretado seguido pasa a la letra siguiente (arriba se ven las letras de la tecla con la elegida marcada), **0** es el espacio y **1** los signos (. , ? ! ' - @ / :). **Rojo** borra una letra, **verde** borra todo, **amarillo** pasa de minúsculas a MAYÚSCULAS y a números, ◀ ▶ mueven el cursor, **OK** acepta (busca) y **Atrás** sale del campo. Desde el celular es más cómodo: escribís en su teclado (ver [Control desde el celular](#control-desde-el-celular)).
- **Ir a una dirección o buscar**: **0** (o el rojo) abre un cuadro «Ir a» arriba de la página: lo que escribís, si parece una dirección (`clarin.com`) se abre, y si no se busca en Google (`search` cambia el buscador, con `{q}` en lugar del texto).
- **Rendimiento en la Pi**: las mismas preferencias que Netflix (sin animaciones, sin trabajo en segundo plano, sin avisos ni ventanas nuevas: los enlaces «en otra ventana» van en la misma), las páginas al **150 %** (`scale`, para leer desde el sillón; `1.0` = como una PC) y protección contra rastreadores estricta (menos cosas que cargar). Lo que más pesa en las páginas es la publicidad: si el Firefox de siempre tiene **uBlock Origin**, tele-cec lo copia al perfil del Navegador y queda habilitado solo (y se actualiza cuando se actualice ese; `ublock = false` lo evita). Otras extensiones: `extensions = ["~/Descargas/algo.xpi"]`.

| Tecla | Recorriendo la página (`navegador`) | Escribiendo en un campo (`navegador_text`) |
|---|---|---|
| ▲ ▼ ◀ ▶ | Al enlace o botón más cercano (al llegar al borde, baja la página) | ▲ ▼ recorren las sugerencias del buscador (si las hay; si no, salen del campo) · ◀ ▶ mueven el cursor |
| OK | Abrir (en un campo de texto: escribir ahí) | Aceptar: buscar / ir (Enter) |
| Atrás | Página anterior (en la primera: cerrar el Navegador) | Salir del campo (cierra «Ir a») |
| 0 | Escribir una dirección o qué buscar | Espacio |
| 1 | ★ Guardar la página en los sitios (otra vez: sacarla) | Signos (. , ? ! ' - @ / :) |
| 2 – 9 | — | Las letras de cada número |
| 🔴 Rojo · 🟢 Verde | Escribir una dirección · recargar | Borrar una letra · borrar todo |
| 🟡 Amarillo | La lista de sitios (ahí, 1 saca el resaltado) | abc → ABC → 123 |
| 🔵 Azul | La página de inicio | — |
| CC | Modo mouse (otra vez: volver al marco) | Igual |
| Canal ± · ⏪ ⏩ | Una página entera arriba o abajo | Igual |
| ▶ Play · ⏸ Pausa | El video de la página | Igual |

```toml
[apps.navegador]
engine = ""                   # "" = el de Netflix; o "firefox" / "chromium"
profile = ""                  # "" = ~/.config/firefox-navegador
home = ""                     # "" = la página propia con los sitios
search = "https://www.google.com/search?q={q}"
scale = 1.5
ublock = true                 # copiar el uBlock Origin del Firefox de siempre
extensions = []               # otros .xpi para copiar al perfil
bookmarks = [                 # hasta que se guarde o se saque uno desde la TV
  { name = "Google", url = "https://www.google.com" },
  { name = "Wikipedia", url = "https://es.wikipedia.org" },
]
```

**OK** hace un clic de verdad (como con un mouse) en un punto que caiga sobre el enlace: el centro, cada renglón si el enlace ocupa dos, o lo de adentro (el título) si algo le tapa el centro. Si aun así el clic no le llegó, le hace uno por script. Mantener apretada una flecha sigue moviendo el marco; mantener OK no abre dos veces.

**Las flechas**, más en detalle: van al enlace o botón cuyo centro queda de ese lado; con ◀ ▶ primero lo que está en la misma fila, y otra fila solo si está del todo a ese lado y cerca (si no, no se mueve: antes, desde el «+» del buscador de Google, ◀ saltaba al pie de la página); con ▲ ▼ se saltea el enlace que va al mismo lugar que el actual (el título y la dirección de un mismo resultado). Si la página pone el foco sola en un campo (Google, al abrir), el marco aparece ahí y los números ya escriben.

**Google y el «tráfico inusual»**: Google puede contestar las búsquedas con su página «Nuestros sistemas detectaron tráfico inusual» y un «No soy un robot», porque Firefox avisa que está bajo control remoto (`navigator.webdriver`; en Netflix tele-cec lo oculta, en el Navegador no). Ese captcha está en un iframe, adonde el marco no llega: con el **modo mouse** (CC) se puede tildar. Si pasa seguido, `search = "https://duckduckgo.com/?q={q}"` busca en DuckDuckGo.

## Juegos (MAME)

**Juegos** lista las ROMs de MAME con su nombre de verdad («Bubble Bobble», «Wonder Boy»), con el año y el fabricante, y abre la que elijas en pantalla completa.

- **Dónde busca**: en el `rompath` de `~/.mame/mame.ini` (o `[apps.juegos] rompath`). Lista los `.zip` / `.7z` (y las carpetas) que MAME reconoce como juegos: los BIOS y los archivos que no son de MAME no aparecen. La primera vez le pregunta los nombres a MAME (`mame -listxml`) y los guarda en `~/.cache/tele-cec/mame-juegos.json`, así después la lista aparece al instante. Las ROMs nuevas se agregan solas.
- **Elegir**: OK (o ▶) sobre «Juegos» muestra la lista; OK sobre un juego lo abre.
- **Jugando**, el joystick es del juego (Start, Back = moneda…), salvo el **botón del medio**: como en las demás apps, abre nuestro menú. Encima del juego aparece la lista con **Seguir jugando**, **Salir del juego**, **Opciones de MAME** (el menú propio de MAME: controles, video, trucos), **Otras actividades** y los demás juegos. Mientras está abierta, **el juego queda en pausa**. **Atrás** del control hace lo mismo.
- Para eso MAME arranca con dos cosas de tele-cec: un archivo de controles donde «mostrar el menú de MAME» queda solo en Tab (no en el botón del medio), y un script Lua que permite pausarlo y cerrarlo bien (guardando lo que MAME guarda al salir: controles, récords). `guide_menu = false` y `control = false` los desactivan.
- Si salís desde MAME (Esc), vuelve la lista de juegos.
- MAME usa su configuración de siempre (`~/.mame`): pantalla completa, video, controles.

```toml
[apps.juegos]
rompath = []          # [] = el de ~/.mame/mame.ini; o p. ej. ["~/mame/roms", "/media/usuario/USB/roms"]
extra_args = []       # más opciones de MAME, p. ej. ["-skip_gameinfo"]
guide_menu = true     # el botón del medio abre nuestro menú (el de MAME queda en Tab y en «Opciones de MAME»)
control = true        # script Lua para pausar y cerrar MAME bien
```

## Android

La entrada **Android** del menú muestra la **pantalla de un celular o tablet Android** en la TV con [scrcpy](https://github.com/Genymobile/scrcpy), y la maneja con el control. Con Android 11 o más nuevo también pasa el **sonido** (sale por la TV y deja de sonar en el celular; con Android 13+ y `audio_dup = true` suena en los dos). No hace falta instalar nada en el celular.

**Instalación**: `./install.sh` instala `adb` y compila scrcpy 5.0.1, que no está en los repositorios de Debian trixie (tarda unos minutos en una Pi 400). La entrada aparece en el menú cuando están `scrcpy` y `adb`.

**Preparar el celular** (una sola vez):

1. *Ajustes › Acerca del teléfono*: tocá 7 veces **Número de compilación** hasta que diga que sos desarrollador.
2. *Ajustes › Sistema › Opciones de desarrollador*: activá **Depuración USB**. En los Xiaomi, además, **Depuración USB (ajustes de seguridad)**: sin eso se ve la pantalla pero no anda el control.
3. Enchufá el celular a la Pi con un cable USB de datos y abrí el menú: el celular pregunta **«¿Permitir depuración USB?»**. Marcá *Permitir siempre desde esta computadora* y aceptá. Mientras tanto, «Android · conectar» dice «Aceptá «Permitir depuración USB» en el celular»; la lista se actualiza sola y, apenas aceptás, muestra la pantalla del celular.

**Por Wi‑Fi** (sin cable, Android 11 o más nuevo), todo con el control: la Pi y el celular tienen que estar en la misma red.

1. En el celular, *Opciones de desarrollador › **Depuración inalámbrica***: activala y entrá.
2. En la TV, elegí **Android** en el menú. Sin celular conectado se abre **«Android · conectar el celular»**; también se llega con **▶** sobre Android.
3. Elegí una de las dos formas:
   - **Vincular por Wi‑Fi con código QR**: la TV muestra un QR. En el celular tocá *Vincular dispositivo con código QR* y apuntá la cámara a la TV.
   - **Vincular por Wi‑Fi con código**: en el celular tocá *Vincular dispositivo con código de vinculación* y escribí con los números del control los 6 dígitos que muestra (◀ borra).
4. tele-cec vincula el celular (`adb pair`), lo conecta y muestra su pantalla.

Se vincula una sola vez. Después, con la depuración inalámbrica activa, elegir Android lo encuentra en la red y lo conecta solo. En «Android · conectar» aparecen los celulares conectados (OK muestra ese) y «Buscar otra vez». El adb de Debian no busca celulares en la red (mDNS), así que tele-cec los busca con `avahi-browse` y dibuja el QR con `qrencode` (los instala `./install.sh`).

Otras formas, desde una terminal en la Pi:

- Con el celular enchufado una vez: `adb tcpip 5555` en la Pi, desenchufalo y poné su IP en la configuración (la ves en *Ajustes › Acerca del teléfono › Estado*). Al elegir Android, tele-cec hace `adb connect` solo. Después de reiniciar el celular hay que repetir `adb tcpip 5555` con el cable.
- A mano: `adb pair IP:PUERTO CÓDIGO` (lo que muestra *Vincular con código*) y después `adb connect IP:PUERTO` (el de la pantalla anterior, que cambia cada vez que se activa).

```toml
[apps.android]
serial = "192.168.0.50:5555"   # "" = el que esté conectado (primero el del cable)
```

| Tecla | Qué hace |
|---|---|
| ▲ ▼ ◀ ▶ · OK | Moverse y elegir (como un teclado conectado al celular) |
| Atrás | Atrás (el botón de Android) |
| Azul | Inicio de Android |
| Rojo | Apps recientes |
| Amarillo | Notificaciones (otra vez: cerrarlas) |
| Menú › OK sobre Android | **Seguir viendo**, **Dejar de compartir la pantalla** u otro celular (el menú ya abre parado en Android) |
| Verde | Lo mismo, si tu TV manda el verde por CEC (muchas lo usan para sus propias funciones) |
| ▶ Play · ⏸ Pausa · ■ Stop | Play · pausa · stop de lo que suena en el celular |
| Canal ± · ⏩ ⏪ | Siguiente / anterior · adelantar / atrasar (si la app los entiende) |
| 0 – 9 · Borrar | Escribir números, por ejemplo el PIN para desbloquear |

- Las flechas, OK y los números se le escriben a la ventana de scrcpy (`wtype`), que se los pasa al celular como un teclado. En muchas apps hechas para el dedo, las flechas recorren los botones con un recuadro; donde no llegan, el celular sigue andando con la mano.
- Atrás, Inicio, Recientes y las teclas multimedia van directo al celular con `adb shell input keyevent`, por un shell de adb que queda abierto mientras se usa Android.
- **Dejar de compartir**: mientras se ve el celular, **Menú › OK** (Android ya está resaltado) abre «Android · conectar» con **Seguir viendo** y **Dejar de compartir la pantalla** arriba de todo. Dejar de compartir cierra scrcpy y queda la lista, para elegir otro celular o volver al menú con Atrás. También se llega con ▶ sobre Android o con el verde.
- **La página de Android en la Ayuda** tiene estas teclas y, al final, los pasos para conectar el celular.
- Si la imagen se ve negra, desbloqueá el celular (con los números escribís el PIN). `stay_awake = true` evita que se apague mientras está enchufado; `turn_screen_off = true` apaga su pantalla mientras se ve en la TV.
- **Rendimiento**: la imagen llega en H.264 y se limita a 1920 píxeles del lado largo y 30 cuadros por segundo (`max_size`, `max_fps`). Si por Wi‑Fi se corta, bajá `video_bit_rate` a `"4M"`.
- Lo que escribe scrcpy queda en `~/.cache/tele-cec/android.log`. Si no abre, el menú muestra por qué (celular sin autorizar, no encontrado…). `tele-cec diagnostico` dice qué celular ve.

```toml
[apps.android]
serial = ""                   # o "IP:5555" por Wi‑Fi
max_size = 1920               # lado largo de la imagen (0 = el del celular)
max_fps = 30
video_bit_rate = ""           # "" = 8M; p. ej. "4M"
audio = true                  # el sonido del celular por la TV (Android 11+)
audio_dup = false             # Android 13+: que suene también en el celular
stay_awake = true
turn_screen_off = false
extra_args = []               # más opciones de scrcpy
```

## Control desde el celular

Lo más fácil: abajo del menú de actividades de la TV hay un **código QR**; apuntale con la cámara del celular y abre la página (lleva la IP de la Pi, así anda aunque el celular no resuelva nombres `.local`; necesita `qrencode`, y `[web] menu_qr = false` lo oculta). O, con el celular en la misma red que la Pi, abrí **`http://raspberrypi.local:8765/`** en el navegador. Si el celular no resuelve nombres `.local`, usá la IP de la Pi, por ejemplo `http://192.168.1.50:8765/`. En el menú del navegador, **Agregar a la pantalla de inicio** la deja como una app más. El botón **⛶** de arriba a la derecha la pone en **pantalla completa**, sin las barras del navegador: queda prendida hasta que la sacás con el mismo botón. El navegador no deja que una página entre sola en pantalla completa: al volver a abrirla, al recargarla o después del «atrás» del celular, vuelve con el primer toque. Con `[web] fullscreen_prompt = true` (viene apagado), en esos casos aparece una cortina que dice «Tocá para pantalla completa», para que ese primer toque no apriete también lo que está abajo. No aparece si el navegador no lo permite (Safari en el iPhone) o si ya la abriste como app. `#tidal`, `#musica` o `#sonido` al final de la dirección abren directo esa parte.

Si se instala una versión nueva en la Pi, la página abierta en el celular se recarga sola.

La página es el mismo **menú de actividades** de la TV, con sus mismos colores: las mismas apps, con su ícono, el texto de abajo y la etiqueta «en uso» en la que está abierta.

- **Tocar una app** hace lo mismo que elegirla con OK en la TV: la abre y, si la Pi está en reposo, la despierta. **Videos** abre su carpeta para elegir el video y **Música**, Explorar (como OK en la TV: no arrancan con todo). Tocar la que está **en uso** ofrece cerrarla (vuelve el menú en la TV) o sus opciones.
- **El botoncito de la derecha**, como «Carpetas ▸» / «Conectar ▸» en la TV:
  - **Videos y música**: abre una página con Videos y Música. **Videos, Fotos** · *Carpetas*: elegir carpeta, entrando en las subcarpetas (en Fotos, también «★ Favoritas» y «Un día como hoy»). Solo las configuradas: no se puede abrir cualquier carpeta de la Pi.
  - **Radio** · *Radios*: la lista de emisoras, con la que suena (y el tema) o la última marcada; tocar una la pone. Tocar la fila pone la última que sonó, como OK en la TV (la primera vez, la lista). Con Fotos en la TV, tocar su fila también ofrece **★ Guardar esta foto en Favoritas** y **⤓ Bajar al celular lo que se ve** (ver abajo).
  - **Navegador** · *Sitios*: arriba, un cuadro para escribir **una dirección o qué buscar** (se abre en la TV; si el Navegador está cerrado, lo abre ahí), y abajo los sitios guardados, con el que está abierto marcado. Con el Navegador en la TV y un campo de texto enfocado (OK sobre él), lo que escribís en el cuadro de texto del celular va a ese campo.
  - **Música** · *Explorar*: tu música local, como Tidal (ver abajo).
  - **Juegos** · *Juegos*: elegir el juego, o «Salir del juego».
  - **Android** · *Conectar*: tocar un celular para verlo en la TV, o «Dejar de compartir la pantalla». Vincular uno nuevo por Wi‑Fi sigue siendo desde el menú de la TV (el QR).
  - **Netflix, YouTube** · *Buscar*: escribís en el celular y los resultados aparecen en la TV. Si la app estaba cerrada, se abre. En Netflix va directo a la búsqueda; en «¿Quién está viendo?» primero hay que elegir el perfil. En YouTube abre su búsqueda con el texto puesto (tarda unos segundos porque reabre YouTube: escribir con el teclado no sirve ahí).
  - **Tidal** · *Explorar*: tu cuenta (ver abajo).
- **Control remoto**: el botoncito redondo con forma de control, abajo a la derecha, abre las teclas del control de la TV en una ventanita que flota encima de la lista (lo que quede tapado se alcanza con scroll), para lo que no se maneja desde las listas (moverse por Netflix, YouTube, Tidal, el Navegador o Android). Tiene las **flechas y OK** (mantener apretada una flecha la repite), **Menú**, **Atrás**, **CC** y **?** (la ayuda en la TV), **Canal ±** a la derecha y, a la izquierda, **★** (como el 1 del control: «Me gusta» del tema que suena en Tidal o del video de YouTube, agregar o quitar de Mi lista en Netflix, guardar la foto en Favoritas, o guardar o sacar la página de los sitios del Navegador; en las demás actividades se ve apagado) y **0** (en Tidal y en un video de Netflix cambia lo que hacen las flechas), los colores **rojo, amarillo y azul** (el verde casi no se usa: sigue en el control de la TV) y **⏪ ⏯ ■ ⏩**. Las teclas hacen lo mismo que en el control de la TV (ver [Teclas por defecto](#teclas-por-defecto)). **Manteniendo apretado** un botón medio segundo aparece un cuadrito con lo que hace ahí mismo (lo mismo que dice la ayuda de la TV en ese momento, por ejemplo «Rojo · Seguir viendo» en Netflix, o «Acá no hace nada»); al soltarlo no se aprieta. Las flechas y Canal ± igual repiten mientras se mantienen, y el cuadrito se ve mientras tanto. Con Fotos en la TV, arriba aparece **⤓** para bajar la foto o el video al celular (ver abajo). El teclado (en Netflix, YouTube, Tidal, el Navegador y Android) abre un cuadro para **escribir en la TV** con el teclado del celular, con ⌫ y Enter. Se abre solo al elegir una actividad de `[web] remote_apps` (Netflix, YouTube, Tidal, Navegador y Android; `[]` = nunca solo) y se cierra solo al pasar a otra que no lo usa. Si lo cerrás con ⌄, no vuelve a abrirse solo hasta que cambies de actividad.
- **Bajar una foto o un video al celular**: con Fotos en la TV, **⤓** en el control (o tocar la fila de Fotos ▸ «⤓ Bajar al celular lo que se ve») muestra la foto o el video que se ve en ese momento, con su carpeta. Queda fijo aunque la presentación pase a otro. **Descargar** lo guarda en el celular. Para mandar una foto por WhatsApp u otra app, mantenela apretada: el navegador ofrece «Compartir imagen» (y «Descargar imagen»). Un video se comparte desde la galería o Descargas después de bajarlo; en la vista se puede mirar antes con ▶, y solo se trae lo que se mira. El botón «Compartir» directo de la página no es posible porque los navegadores lo permiten solo en páginas `https`, y esta es `http`. Lo que pase de `[web] share_max_mb` (150 MB; 0 = nada) no se baja: avisa «El archivo es demasiado grande para compartir». Solo se bajan archivos de las carpetas configuradas en `[apps.fotos] folders`, nunca otro de la Pi. Los archivos grandes se mandan de a pedazos, sin cargarlos enteros en la memoria de la Pi.
- **Temporizador para apagar**: en el reproductor grande y en la página Sonido; dice cuánto falta y se cambia o se saca desde ahí (o desde Sonido en la TV).
- **Sonido**: en la lista de actividades, abajo de «Sonido», la **barra de volumen** (tocar o arrastrar) con el **silencio** al lado; tocar la fila abre por dónde sale (la TV por HDMI o un parlante Bluetooth ya emparejado; tocarlo lo conecta), con la misma barra arriba. Emparejar uno nuevo sigue siendo desde «Sonido» en el menú de la TV. Con la TV por HDMI, este volumen es el de la Pi: la TV tiene el suyo aparte.
- **Abajo, lo que suena** (Tidal, Videos, Música, Netflix o YouTube) con ⏯ y ⏭. Tocarlo abre el reproductor grande: la tapa, la barra (tocar o arrastrar para ir a otra parte del tema), ⏮ ⏯ ⏭, **aleatorio** y **repetir** (la lista o un tema; prendidos se ven resaltados, con Tidal y con Música), ★ «Me gusta» en Tidal y el volumen. Abajo de todo, **En la pantalla bloqueada**: prendido, los controles de la música aparecen en la notificación y con el celular bloqueado (tapa, tema, ⏮ ⏯ ⏭ y la barra), como con cualquier app de música, y manejan lo que suena en la Pi. Para eso la página reproduce un sonido mudo: toma el audio del celular (pausa otra música que suene en el teléfono), por eso viene apagado. Queda prendido en ese celular; después de recargar la página arranca con el primer toque. Las teclas de volumen siguen siendo las del celular: el navegador no se las pasa a la página. Anda en Android con Chrome y con Firefox; en el iPhone puede cortarse al bloquearlo. Maneja ese reproductor aunque en la TV haya otra cosa, por ejemplo la música de Tidal mientras pasan las fotos.

**Tidal · Explorar**: tu cuenta, a través de High Tide (una sola sesión y una sola cola: lo que elegís en el celular se ve en la TV, y al revés).
- **Mis canciones, Playlists, Mixes** (los guardados y los que arma Tidal para vos), **Discos** y **Artistas**. Tocar un tema lo pone a sonar desde ahí. Tocar una playlist, mix, disco o artista muestra sus temas, con **▶ Reproducir** y **🔀 Mezclar**.
- **Buscar**: temas, artistas, discos y playlists.
- **⋯** en un tema: reproducirlo ahora, que suene después del actual o agregarlo al final de la cola.
- **Cola**: lo que suena, lo agregado a mano y lo que sigue. Tocar un tema salta a él.
- Si Tidal está cerrado, poner algo lo abre (unos 10 s). Con el sonido en un parlante Bluetooth no prende la TV ni le cambia la entrada: es solo música.

**Música · Explorar**: la música de las carpetas de Música (`~/Música` o un pendrive), igual que Tidal.
- **Artistas**, **Discos** y **Temas**, ordenados por las etiquetas de los archivos (título, artista, disco, número; con `python3-mutagen`) o, si no las tienen, por las carpetas: `Artista/Disco/01 Tema.mp3`. Las tapas salen del archivo o de `cover.jpg` / `folder.jpg`. Tocar un artista o un disco muestra sus temas, con **▶ Reproducir** y **🔀 Mezclar** (prende el aleatorio sin cortar el tema elegido: apagarlo vuelve al orden del disco); en Temas, también toda la música.
- **Carpetas**: elegir una carpeta, como en la TV.
- **Buscar** (sin importar tildes ni mayúsculas): temas, artistas y discos. Tocar un tema pone su disco desde ese tema.
- **⋯** en un tema: reproducirlo ahora, que suene después del actual o agregarlo al final de la cola.
- **Cola**: lo que suena y lo que sigue. Tocar un tema salta a él.
- Si Música está cerrada, poner algo la abre con esos temas; si ya está sonando (también de fondo, detrás de las fotos), le cambia la lista sin cerrarla. Con el sonido en un parlante Bluetooth no prende la TV.
- La primera vez lee las etiquetas de todos los temas (unos segundos cada mil temas) y las guarda en `~/.cache/tele-cec/musica-indice.json`; después solo lee los nuevos.

**Parlante Bluetooth y Wi‑Fi**: en la Pi 400 el Wi‑Fi y el Bluetooth comparten el chip. Si la Pi usa Wi‑Fi de 2,4 GHz, el sonido por Bluetooth se entrecorta mientras el celular usa la página. Con la Pi por cable conviene apagar su Wi‑Fi (`sudo nmcli radio wifi off`) o usar 5 GHz.

```toml
[web]
enabled = true     # false: sin página
port = 8765
lan_only = true    # solo la red de la casa
volume_step = 5
```

**No tiene contraseña.** Solo atiende a direcciones de la red local y a nombres de la casa (`.local`, la IP o el nombre de la Pi). Los pedidos que cambian algo necesitan un encabezado propio, así una página de internet abierta en el celular no puede manejar la TV. Cualquiera que esté en tu Wi‑Fi sí puede usarla.

## Tidal

Tidal se maneja con **[High Tide](https://github.com/Nokse22/high-tide)**, un cliente de Tidal para Linux que se instala con Flatpak (también para la Pi, aarch64). No es la web de Tidal: la web bloquea el inicio de sesión cuando el navegador está bajo control remoto, y High Tide además es mucho más liviano (en una Pi 400, ~9 % de un núcleo reproduciendo en calidad lossless).

**Instalación:** `./install.sh` (salvo con `--sin-tidal`). Instala High Tide para tu usuario, `playerctl`, `wtype` y la accesibilidad, y en labwc (Raspberry Pi OS) agrega el portal de secretos en `~/.config/xdg-desktop-portal/labwc-portals.conf`, sin el cual High Tide se queda en «Cargando…» y no guarda la sesión.

**Una vez, a mano:**
1. **Llavero sin contraseña**: como la Pi entra sola al escritorio, un llavero con contraseña la pediría en cada arranque, y con el control no se puede contestar. Abrí «Contraseñas y claves» (`sudo apt install seahorse`), **+ → Llavero de contraseñas**, sin contraseña, y **clic derecho → Establecer como predeterminado**. Si había otro llavero bloqueado que no usás, borralo: si no, High Tide puede pedir su contraseña.
2. **Iniciá sesión en High Tide** (abrilo desde el menú de actividades; con el teclado de la Pi seguís el enlace de Tidal). Queda guardada.
3. `tele-cec diagnostico` → sección **Tidal**: tiene que estar todo en OK.

**Cómo funciona por dentro:** cambiar de app cierra la otra ordenadamente (Firefox con SIGTERM, High Tide por MPRIS) para que nunca corran las dos. Los controles van por **MPRIS** (`playerctl`); cambiar de pantalla, "me gusta", las letras y "Mis canciones" van por el **canal de control** que abre el parche dentro de High Tide (`control.sock` en su carpeta de datos): preguntarle por **accesibilidad** (AT-SPI) mientras dibuja puede cerrarlo (error de GTK 4), así que la accesibilidad queda solo de respaldo si el parche está desactivado; y lo que solo responde al teclado (Enter sobre un tema, las flechas del modo navegación) va con **`wtype`** a la ventana de High Tide, que es la única abierta. Hay un modo experimental de flechas que sigue el foco por accesibilidad (`music:move`, `apps/tidal/nav_helper.py`), pero no se usa por defecto: escuchar los eventos de accesibilidad hace que High Tide (GTK 4) se cierre a veces. "Mis canciones" = Colección → Canciones y reproduce todos tus favoritos; el amarillo mira qué pantalla muestra High Tide para seguir el ciclo Mis canciones → Colección → Inicio.

```toml
[apps.tidal]
on_reselect = "music:my_tracks"   # app:tidal con Tidal ya abierto
resume_on_open = false            # true = al abrir, seguir con lo último que sonaba (si no: Inicio, en pausa)
start_in_nav = true               # al abrir, las flechas recorren la interfaz (0 = controles)
volume_step = 0.05
gsk_renderer = "ngl"              # OpenGL: con Vulkan, en la Pi High Tide se cierra solo al cambiar de vista
```

## Firefox y Chromium

tele-cec usa **Firefox** por defecto. **Chromium** es la alternativa:

```bash
./install.sh --chromium                 # o --chromium --copiar-perfil para no volver a iniciar sesión
```

O, en tu `config.toml`:

```toml
[browser]
engine = "chromium"                     # "firefox" es el de por defecto
```

Funcionan igual (mismas teclas, contextos y acciones). Con Firefox cambia esto:

- **Protocolo**: Firefox ya no tiene DevTools (CDP). Se controla por **WebDriver BiDi**, el estándar que lo reemplaza, por el mismo `--remote-debugging-port`. Los clics y las teclas también llegan como "reales".
- **Una sola sesión de control**: Firefox admite un solo cliente a la vez, y lo es el servicio. Por eso `accion`, `diagnostico` y `navegador` se lo piden al servicio por su socket local. Si el servicio no está corriendo, se conectan por su cuenta y cierran la sesión al terminar.
- **Sesión colgada**: si un cliente muere sin cerrar la sesión (por ejemplo, con `kill -9`), Firefox no la libera nunca. El servicio lo detecta y reinicia ese Firefox, el del perfil `firefox-netflix`. Tu sesión de Netflix se conserva porque está en el perfil.
- **Arranque lento** (Raspberry Pi): Firefox abre el puerto de control enseguida, pero recién acepta la sesión cuando terminó de abrir la ventana, y eso puede tardar. tele-cec espera hasta 90 s, y si deja de esperar antes, igual cierra esa sesión apenas Firefox responde, para que no quede tomada.
- **Perfil y preferencias**: el perfil es `~/.config/firefox-netflix`. Cada vez que abre Firefox, tele-cec le escribe un `user.js` con lo necesario: DRM, reproducción automática, sin carteles de primer inicio ni de restaurar sesión, y sin el botón de imagen en imagen sobre el video. También desactiva las "preferencias recomendadas" del agente remoto, que son para pruebas automáticas (apagan Safe Browsing, el guardado de contraseñas y la protección contra rastreo). Agregá las tuyas en `[browser.firefox] prefs`.
- **`navigator.webdriver`**: con el control remoto activo, Firefox le avisa a las páginas que está automatizado (Chromium no). tele-cec lo oculta para que Netflix vea el mismo navegador de siempre. Se puede desactivar con `[browser.firefox] hide_webdriver = false`.
- **Barra a rayas con un robot**: es el aviso de Firefox de que está bajo control remoto. En modo kiosko no se ve.
- **Extensiones**: si copiaste tu perfil con `--copiar-perfil`, vinieron tus extensiones (uBlock Origin, h264ify…). En una Pi cada una gasta algo de CPU. `tele-cec diagnostico` las lista, y con `disable_extensions` se apagan solo en este perfil: quedan instaladas, como deshabilitadas en `about:addons`. Si las sacás de la lista, tele-cec las vuelve a habilitar; las que apagaste vos a mano no las toca. Se aplica al abrir Firefox, así que después del cambio cerralo y volvé a abrirlo. En una Raspberry Pi conviene dejar **UANaughtyList**: cambia cómo se identifica el navegador ante sitios que rechazan Linux en ARM, y puede ser lo que hace que Netflix te deje entrar.

```toml
[browser.firefox]
binary = ""                              # "" = firefox / firefox-esr
profile = "~/.config/firefox-netflix"
extra_args = []
prefs = { "media.ffmpeg.vaapi.enabled" = true }
hide_webdriver = true
disable_extensions = ["uBlock Origin", "h264ify"]
```

## Configuración

Archivo: `~/.config/tele-cec/config.toml`. Poné **solo lo que quieras cambiar**: lo demás toma el valor por defecto. Todos los valores por defecto están comentados en `~/.local/share/tele-cec/tele_cec/defaults.toml`.

Después de editar:

```bash
systemctl --user restart tele-cec
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

Las teclas se nombran como en libcec (`UP`, `SELECT`, `EXIT`, `F2_RED`, `NUMBER1`, `CHANNEL_UP`…) o con su número (`"113"`, `"0x71"`). `tele-cec aprender` te muestra ambos.

### Opciones principales

| Clave | Por defecto | Para qué |
|---|---|---|
| `cec.port` | `""` (autodetectar) | `/dev/cec0` = HDMI 0, `/dev/cec1` = HDMI 1 |
| `cec.device_type` | `"r"` | Tipo de dispositivo CEC: `r` recording (como cec-mini-kb) o `p` playback |
| `cec.activate_source` | `"both"` | Cuándo anunciarse como fuente activa (la TV cambia a la Pi): `start`, `launch`, `both`, `never` |
| `cec.pause_on_standby` | `true` | Pausar si se apaga la TV (Música y Tidal siguen si el sonido sale por parlantes Bluetooth u otra salida que no sea la TV) |
| `browser.engine` | `"firefox"` | `firefox` o `chromium` (ver [Firefox y Chromium](#firefox-y-chromium)) |
| `browser.launch` | `"on_key"` | Navegador cerrado: `on_key` (el rojo lo abre), `always` (siempre abierto, salvo mientras está Tidal), `never` |
| `browser.user_data_dir` | `~/.config/chromium-netflix` | Perfil de Chromium propio |
| `browser.firefox.profile` | `~/.config/firefox-netflix` | Perfil de Firefox propio |
| `browser.firefox.disable_extensions` | `[]` | Extensiones a apagar en ese perfil (por nombre o id) |
| `browser.extra_args` | `["--password-store=basic"]` | Flags extra de Chromium (por ejemplo tu `--user-agent`) |
| `browser.kiosk` | `true` | Pantalla completa sin barras |
| `browser.reduce_effects` | `true` | Sin animaciones, transiciones ni scroll suave (ver [Rendimiento](#rendimiento)) |
| `browser.stop_previews` | `true` | Pausar y ocultar los avances que Netflix reproduce solos en el inicio |
| `feedback.busy` · `feedback.busy_delay_ms` | `true` · `250` | Indicador "trabajando" y a partir de cuánto mostrarlo (ver [Avisos](#avisos-mientras-trabaja)) |
| `feedback.key_echo` | `false` | Mostrar cada tecla recibida |
| `feedback.cec_osd` · `feedback.cec_osd_text` | `false` · `"Netflix..."` | Texto en la pantalla de la TV al abrir el navegador (máx. 13 caracteres) |
| `netflix.seek_seconds` | `10` | Salto de ◀ ▶ |
| `netflix.volume_step` | `0.1` | Paso de volumen (10 %) |
| `netflix.default_profile` | `0` | Perfil para "Seguir viendo" (0 = preguntar) |
| `menu.items` · `menu.timeout_s` | las 4 · `10` | Orden del menú de actividades · cierre automático (ver [Menú de actividades](#menú-de-actividades)) |
| `apps.videos.folders` · `apps.musica.folders` · `apps.fotos.folders` | pendrives, después `~/Vídeos` · `~/Música` · `~/Imágenes` | De dónde salen los videos, la música y las fotos (ver [Videos, música y fotos](#videos-música-y-fotos)) |
| `menu.groups.<id>` | `local` = Videos y Música | Filas del menú que abren otra lista: `name`, `icon`, `items` (y el id va en `menu.items`) |
| `apps.fotos.delay_s` | `8` | Segundos por foto |
| `apps.fotos.scroll_step` · `apps.fotos.zoom_step` | `150` · `25` | Con zoom: píxeles por flecha · % de cada paso de zoom |
| `apps.netflix.on_reselect` | `"continue_watching"` | Qué hace `app:netflix` con Netflix ya abierto |
| `apps.tidal.on_reselect` | `"music:my_tracks"` | Qué hace `app:tidal` con Tidal ya abierto |
| `apps.tidal.resume_on_open` | `false` | Al abrir Tidal, seguir con lo último que sonaba (`false`: Inicio, en pausa) |
| `apps.tidal.start_in_nav` | `true` | Al abrir Tidal, las flechas recorren la interfaz (0 cambia a controles) |

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
| `menu` | Abrir el menú de actividades (abierto: volver atrás o cerrarlo; `menu:next` pasa a la opción siguiente) |
| `menu:up` `menu:down` `menu:ok` `menu:close` `menu:pick:N` | Moverse en el menú, elegir, cerrarlo, elegir la opción N |
| `launch` | Abrir Netflix (cierra la otra actividad si hay una abierta) |
| `app:netflix` · `app:videos` … | Abrir esa actividad directo (cerrando la actual) o, si ya está abierta, hacer su `on_reselect` |
| `music:play_pause` `music:play` `music:pause` `music:next` `music:previous` | Tidal: reproducción |
| `music:volume:+` · `music:seek:-10` | Tidal: volumen · saltar segundos |
| `music:like` · `music:shuffle` | Tidal: me gusta · aleatorio |
| `my_list_toggle` | Netflix: ★ agregar o quitar de Mi lista (el título resaltado o la ficha) |
| `yt:like` | YouTube: ★ me gusta en el video (otra vez lo saca) |
| `music:my_tracks` | Tidal: reproducir "Mis canciones" (tus favoritos) |
| `music:nav` · `music:move:Down` · `music:key:Down` · `music:back` | Tidal: cambiar las flechas (recorrer la interfaz ↔ controles) · mover el foco (con Tab de respaldo) · mandar una tecla sola · botón Atrás |
| `music:lyrics` · `music:page:inicio` (`explorar`, `coleccion`) | Tidal: mostrar u ocultar las letras · ir a esa página |
| `my_list` | Netflix: abrir Mi lista |
| `video:ok` `video:play_pause` `video:next` `video:previous` `video:seek:+10` `video:volume:+` `video:back` | Videos (mpv): reproducción, buscar (con la barra), volumen, dejar de buscar |
| `musica:…` (las mismas) · `musica:shuffle` · `musica:loop` | Música local: lo mismo que Videos, y aleatorio (mezclar / en orden) y repetir (la lista, el tema, nada) |
| `video:subtitles` · `video:audio` | Videos: ciclar subtítulos · pista de audio |
| `fotos:next` · `fotos:previous` · `fotos:pause` | Fotos: siguiente, anterior, pausar o seguir la presentación |
| `fotos:rotate` · `fotos:random` · `fotos:info` · `video:aspect` | Fotos: girar, una al azar, mostrar el nombre · Videos: proporción de la imagen |
| `fotos:zoom_in` · `fotos:zoom_out` · `fotos:fit` · `fotos:scroll:left` (`right`, `up`, `down`) | Fotos: acercar (el primero pausa la presentación) · alejar · foto entera · mover la foto con zoom |
| `reload` | Recargar la página |
| `macro:nombre` | Ejecutar una macro de `[macros]` |
| `none` o `""` | Nada (desactiva la tecla) |

Una **macro** es una lista de acciones. Si apretás cualquier tecla mientras una macro está corriendo, la macro se cancela.

## Comandos

```bash
tele-cec aprender [--todo] [--crudo]   # ver las teclas CEC (--todo: todo el tráfico; --crudo: salida de cec-client)
tele-cec diagnostico [--detalle]       # revisar CEC, el navegador y la página actual de Netflix
tele-cec navegador [URL]               # abrir el navegador listo para el control (o traerlo al frente)
tele-cec accion ACCIÓN [ACCIÓN…]       # ejecutar acciones ya mismo, p. ej.: tele-cec accion smart_skip
tele-cec menu                          # mostrar el menú de actividades de ejemplo (para ver cómo queda en la TV)
tele-cec tecla ok rojo …               # mandar teclas al servicio como si vinieran del control (para probar por SSH)
tele-cec servicio [--teclado]          # el servicio (lo usa systemd); --teclado = probar escribiendo teclas
tele-cec --debug …                     # cualquiera de los anteriores con logs detallados
```

`tele-cec accion` sirve para probar desde otra máquina por SSH, por ejemplo `tele-cec accion nav:down ok` o `tele-cec accion menu` (y después `menu:down`, `menu:ok`).

Si el servicio está corriendo, `accion`, `diagnostico` y `navegador` se lo piden a él por un socket local (`$XDG_RUNTIME_DIR/tele-cec.sock`), así que el detalle de lo que pasó queda en su log. Con Chromium, `accion --directo` se conecta al navegador por su cuenta, útil con `--debug` para ver cada paso en la consola. Con Firefox no se puede, porque admite una sola sesión de control.

## Servicio y logs

```bash
systemctl --user status tele-cec
systemctl --user restart tele-cec
journalctl --user -u tele-cec -f            # en vivo
journalctl --user -u tele-cec -p warning    # solo avisos y errores
```

El log registra cada tecla con su contexto y acción:

```
cec: CEC listo (/dev/cec0)
tele: Conectado a Firefox: https://www.netflix.com/browse
tele: Tecla F2_RED (rojo) [browse] -> continue_watching
tele: Seguir viendo: Merlina
tele: Tecla F4_YELLOW (amarillo) [player] -> smart_skip
tele: Botón inteligente: Saltar intro
tele: Tecla ROOT_MENU (menú) [player] -> menu
tele: Menú: [Netflix] · Tidal · Videos · Fotos
tele: Tecla DOWN (flecha abajo) [menu] -> menu:down
tele: Tecla SELECT (OK) [menu] -> menu:ok
tele: Menú: elijo Tidal
tele: Cambio a Tidal
```

El servicio arranca con tu sesión (`default.target`). Con el inicio automático de sesión de Raspberry Pi OS, arranca al encender la Pi.

### Recuperación automática

| Qué pasa | Qué hace tele-cec |
|---|---|
| `cec-client` termina o no logra abrir el adaptador | Lo reinicia con espera creciente (2 s → 60 s) |
| Desaparece `/dev/cecN` o hay una ráfaga de errores CEC | Reinicia `cec-client` |
| `cec-client` se cuelga al arrancar (sin "waiting for input" en 45 s) | Lo mata y reinicia |
| Se cierra o se cae el navegador | Vuelve al contexto `offline`, reintenta conectarse cada 2 s y lo reabre desde el menú, o solo si `launch = "always"` y no hay otra actividad abierta (máximo una vez cada 15 s; tras 5 fallos seguidos espera 5 min) |
| Se cierra o se cae el panel del menú | Se vuelve a abrir la próxima vez que apretás Menú |
| Se recarga la página o Netflix navega | Vuelve a inyectar el script |
| El propio servicio falla | systemd lo reinicia (`Restart=always`) |

| Firefox quedó con la sesión de control tomada (un cliente murió sin cerrarla) | Firefox no la libera nunca: el servicio reinicia ese Firefox, el del perfil `firefox-netflix` (salvo con `launch = "never"`) |

Reiniciar o detener el servicio **no cierra el navegador**, porque se abre en su propio scope de systemd.

## Rendimiento

En una Raspberry Pi, Netflix en Chromium puede andar pesado. tele-cec trae activadas dos opciones que lo alivianan:

- **`reduce_effects = true`**:
  - Abre Chromium con `--force-prefers-reduced-motion` y `--disable-smooth-scrolling` (en Firefox: `ui.prefersReducedMotion` y `general.smoothScroll` en el `user.js`).
  - Inyecta CSS que deja todas las transiciones y animaciones de la página en 1 ms. Las animaciones quedan en 1 ms y no en "nada" para que el código de Netflix que espera el final de una animación (por ejemplo, el paso de página de las filas) siga funcionando.
  - Ese CSS también saca lo que más le cuesta dibujar a la GPU de la Pi: desenfoques (`backdrop-filter`, `filter`), sombras difusas (`box-shadow`, `text-shadow`), capas extra (`will-change`) y fondos fijos. El marco de foco queda con un contorno sólido, sin resplandor.
  - Las animaciones que Netflix hace por JavaScript con `element.animate()` (el CSS no las alcanza) también pasan a durar 1 ms.
  - El resaltado y el scroll pasan a ser instantáneos.
  - `tele-cec diagnostico` muestra si todo esto está activo y qué animaciones siguen corriendo en la página en ese momento.

Independientemente de esta opción, el marco de foco solo sigue al elemento cuadro a cuadro un instante después de cada tecla o scroll; el resto del tiempo se revisa 4 veces por segundo. Al pasar de página una fila o al cerrar una ficha con Atrás no hay esperas fijas: la página avisa apenas la fila dejó de moverse o la ficha se cerró (sin animaciones, unos 100 ms en vez de 250–350 ms). Si preferís una espera fija para el paso de página, usá `netflix.page_delay_ms`. Y si mantenés apretado un botón mientras la página viene lenta, las repeticiones que quedaron esperando más de 0,35 s se descartan, para que el foco no siga de largo después de soltarlo.
- **`stop_previews = true`**: pausa y oculta los videos que Netflix reproduce solos mientras navegás (el avance del inicio y las vistas previas). No afecta al reproductor.

Para volver a los efectos normales, en tu `config.toml`:

```toml
[browser]
reduce_effects = false
stop_previews = false
```

Los flags se aplican al abrir el navegador y el CSS al reiniciar el servicio, así que después del cambio: `systemctl --user restart tele-cec`, cerrá el navegador y volvé a abrirlo (OK en el control). En Firefox, al reabrirlo tele-cec también borra esas preferencias de `prefs.js`, donde Firefox las había guardado, para que vuelvan a su valor normal.

Otras cosas que ayudan bastante:

- **Fuente de alimentación**: con una fuente o un cable flojos, la Pi detecta baja tensión y frena la CPU (una Pi 400 pasa de 1800 a 600 MHz), y Netflix se vuelve lentísimo: tarda más de medio minuto en calmarse después de cargar el inicio y las teclas responden con segundos de demora. `tele-cec diagnostico` lo muestra en "Alimentación". Usá la fuente oficial (5,1 V / 3 A, USB-C) con su cable.

- **Desactivar los avances desde Netflix**: en tu cuenta → Perfiles → (tu perfil) → Configuración de reproducción, destildá "Reproducir avances automáticamente mientras navegas en todos los dispositivos". Es más efectivo que `stop_previews`, porque Netflix ni siquiera los descarga.
- **Resolución 1080p, no 4K**: si la TV es 4K y el escritorio corre a 3840×2160, la Pi dibuja cuatro veces más píxeles. Configurá 1920×1080 en *Preferencias → Configuración de pantalla*. Netflix en Chromium para Linux reproduce como mucho en 1080p (normalmente 720p), así que no perdés calidad de video.
- **Aceleración por GPU**: abrí `chrome://gpu` en Chromium y verificá que diga "Hardware accelerated" en la mayoría de los ítems.

## Solución de problemas

**`aprender` no muestra teclas**
- Cerrá Kodi y elegí en la TV la entrada HDMI de la Pi.
- En la TV, activá HDMI-CEC (Anynet+, Simplink, Bravia Sync, EasyLink…).
- Probá `device_type = "p"` en `[cec]`.
- Si la TV está en el otro HDMI de la Pi 400, probá `port = "/dev/cec1"`.
- Corré `tele-cec aprender --crudo` para ver la salida completa de `cec-client`.

**Algunas teclas aparecen como "Vendor"**: tu TV las manda con un comando propio del fabricante, no con el estándar. Esas teclas no se pueden mapear todavía.

**La TV cambia sola a la Pi al encenderla**: poné `activate_source = "launch"` o `"never"`.

**Chromium no abre**
- Revisá el log y `tele-cec diagnostico`.
- Si ya hay otro Chromium usando `~/.config/chromium-netflix` sin el puerto de depuración, cerralo.
- Para usar tu propio acceso directo, tiene que incluir `--remote-debugging-port=9222 --user-data-dir=$HOME/.config/chromium-netflix`. Lo más simple es usar "Netflix (TV)" o `tele-cec navegador`.

**Netflix no reproduce en el perfil nuevo** (error de DRM o de navegador no compatible): agregá en `browser.extra_args` los mismos flags que usás en tu Chromium habitual, por ejemplo tu `--user-agent`. En Firefox, la primera vez Widevine se descarga solo y tarda un minuto: esperá y probá de nuevo.

**Quiero salir de la pantalla completa**: con `kiosk = true` (por defecto) el navegador no tiene barras y F11 no hace nada. Desde el teclado: **Alt+Tab** cambia a otra ventana y **Alt+F4** cierra el navegador (el control lo vuelve a abrir con OK). Para usarlo en ventana poné `kiosk = false` en `[browser]`: Chromium abre en pantalla completa común (F11 la alterna) y Firefox abre en una ventana normal.

**Algo de Netflix dejó de funcionar** (el botón inteligente no encuentra nada, no detecta los perfiles…): Netflix cambia su web cada tanto.
1. Con esa pantalla abierta, corré `tele-cec diagnostico --detalle`. Lista los atributos `data-uia` visibles y los métodos del reproductor.
2. Ajustá el selector correspondiente en `[selectors]` de tu `config.toml`. La lista completa está en `tele_cec/config.py` (`DEFAULT_SELECTORS`).

**El resaltado se pierde o salta raro**: corré `tele-cec --debug accion nav:down` y `tele-cec diagnostico --detalle` para ver qué elementos considera navegables. Lo que no deba enfocarse se agrega a `selectors.ignore`.

## Seguridad

El puerto de control (DevTools o WebDriver BiDi) escucha **solo en 127.0.0.1**. Cualquier programa que corra en la Pi con tu usuario podría controlar ese navegador, igual que podría leer tus archivos. No abras ese puerto a la red (no uses `--remote-debugging-address=0.0.0.0`). El socket del servicio está en `$XDG_RUNTIME_DIR`, con permisos solo para tu usuario. La [página del celular](#control-desde-el-celular) escucha en la red de la casa (puerto 8765), sin contraseña: `[web] enabled = false` la apaga. Los perfiles `chromium-netflix` y `firefox-netflix` son solo para Netflix.

## Probar sin TV ni Netflix

```bash
python3 tests/e2e_mock.py                    # navegador headless + Netflix simulado + teclas CEC simuladas (76 verificaciones por navegador)
python3 tests/e2e_mock.py --layout nuevo     # solo el diseño actual de Netflix (o --layout viejo)
python3 tests/e2e_mock.py --motor firefox    # solo Firefox (o --motor chromium); por defecto, los que estén instalados
python3 tests/serve_mock.py                  # sirve el Netflix simulado en http://www.netflix.com.localhost:8765/browse (?layout=viejo para el diseño anterior)
python3 tests/apps_test.py                   # menú de actividades con Netflix, Tidal, mpv, feh y el panel simulados
tele-cec servicio --teclado               # escribir teclas por consola: u d l r ok b m (menú) rojo verde amarillo azul 1..9
```

### Cómo está organizado el código

```
tele_cec/
├── __main__.py, cli/      línea de comandos: servicio, aprender, accion, navegador, diagnostico, tecla, menu
├── config.py, keys.py     configuración (defaults.toml + tu config.toml), contextos y nombres de teclas CEC
├── controller/            el cerebro: tecla → contexto → acción
│   ├── core.py            cola de teclas, contexto, acciones y macros
│   ├── browser.py         conexión con el navegador de Netflix y primitivas sobre la página
│   ├── netflix.py         acciones de Netflix          media.py   acciones de Videos, YouTube, Fotos, Android y Tidal
│   ├── activities.py      cambio de actividad          menu.py    menú, carpetas, sonido y ayuda
│   ├── sleeptimer.py      temporizador para apagar
│   └── sleep.py           reposo: guardar, cerrar y volver
├── apps/                  las actividades del menú
│   ├── netflix/           navegador (cdp.py, bidi.py), script inyectado (nfx.js)
│   ├── youtube/           YouTube para TV (y la extensión H.264 para Chromium)
│   ├── tidal/             High Tide: control, parche que se le carga y accesibilidad
│   ├── videos.py, music.py, player.py, mpv.py   Videos y Música con mpv (mpv_scripts/: la barra y thumbfast)
│   ├── library.py         la música local por artista, disco y tema (para el celular)
│   ├── fileinfo.py        lo que dicen los temas y los videos, para las listas del menú
│   ├── resume.py          Seguir viendo: dónde quedó cada video (lo guarda mpv)
│   ├── photodates.py      la fecha y el GPS de cada foto (nombre o EXIF), para «Un día como hoy» y el mapa
│   ├── photomap.py        el mapa de dónde se sacó una foto (mosaicos de OpenStreetMap)
│   ├── photos.py, media.py, folders.py   Fotos (mpv, o feh) y las carpetas de contenido
│   ├── android.py         la pantalla del celular (scrcpy y adb)
│   └── base.py, webprofile.py                        base de las actividades y perfiles de navegador
├── system/                procesos, CEC, sonido (audio.py), reposo (power.py), MPRIS y teclas sintéticas
└── ui/                    panel del menú (menu_ui.py, proceso GTK aparte), su cliente, la ayuda y los íconos
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
