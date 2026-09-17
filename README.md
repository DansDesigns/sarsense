# SARSense
Dedicated to Noah Woods & all missing children, I hope this helps bring you home.

Wi-Fi sensing for search and rescue. Cheap ESP32 boards listen to how Wi-Fi signals change as people move or breathe, a small hub turns that into positions on a map, and anyone on the network can follow along in a phone browser.

People found inside a **tagging zone** get an anonymous ID like `U007`. Responders who register on their phone show up by name instead, so a volunteer walking the area is not mistaken for a missing person.

It builds on the ideas in TOMMY (ESP32 CSI presence sensing), the Raspberry Pi Nexmon write-up (correlation-based motion score), the presence-detection CNN work, and the CMU DensePose-from-Wi-Fi paper. That last one needs lab radios and a GPU, so it is out of reach here. What you get is presence, a coarse position (metres, not centimetres) and a "possibly breathing" flag.

## What runs where

| Part | Hardware | Job |
|---|---|---|
| Sensor node | ESP32, S3, C3, C5, C6 (C5/C6 best for still people) | Captures CSI, forwards it over UDP |
| Pi node (optional) | Raspberry Pi 3B+/4B/5 with nexmon_csi | Same job, via `tools/nexmon_forward.py` |
| Hub | NanoPi NEO Air (512 MB), any Linux box, or Windows | Detection, tracking, map, web app |
| Clients | Any phone or laptop browser | View, draw zones, check in as a responder |

The NanoPi NEO Air's own Wi-Fi chip cannot capture CSI, and neither can phones or Windows laptops. That is why the sensing is done by ESP32s and everything else talks to the hub.

## Quick start

Needs Python 3.9 or newer. The installer puts everything in a `.venv` folder inside the project, so nothing is installed system-wide and the whole folder can live on a USB stick.

**Linux / macOS**

```
./install.sh
./run_demo.sh                 # try it with simulated sensors
./run_hub.sh --pin 4821       # run a real hub
```

`install.sh` also adds *SARSense Hub*, *SARSense Demo* and *SARSense Web App* to the desktop's app menu (under Internet or Network). The hub and demo open in a terminal window that stays open after they stop, so any error can be read. Entries go in `~/.local/share/applications`, or `/usr/local/share/applications` when run as root. They point at the project folder, so run `./install.sh` again if you move it.

**Windows:** install Python from python.org, then double-click `install.bat`. It adds a **SARSense** folder to the Start menu with *SARSense Hub*, *SARSense Demo*, *Open SARSense* (the web app), the project folder, and *Remove SARSense shortcuts*. `run_hub.bat` and `run_demo.bat` in the folder do the same as the shortcuts and call the installer themselves if `.venv` is missing. When Windows asks, allow Python through the firewall on private networks (UDP 5566 for sensors, TCP 8080 for phones).

For the demo, open <http://localhost:8080> and unlock operator mode with PIN `1234` on the Hub tab. The simulator places 6 sensors and a router, draws a zone, walks two people around (one of them a registered responder) and leaves one person lying still near a sensor link.

### Installer options

| `install.sh` | `install.bat` | Does |
|---|---|---|
| `--recreate` | `--recreate` | Delete `.venv` and build it again |
| `--test` | `--test` | Run the test suite afterwards |
| `--python PATH` | | Use a specific Python |
| `--system-numpy` / `--no-system-numpy` | | Share the system NumPy, or install a private copy |
| `--no-menu` | `--no-shortcuts` | Skip the app menu or Start menu entries |
| `--remove-menu` | `--remove-shortcuts` | Remove those entries and stop (the folder is kept) |
| | `--desktop` | Also put a *SARSense Hub* shortcut on the desktop |
| | `--no-pause` | Don't wait for a key at the end |

On Linux the installer reuses the system NumPy when one is installed, which matters on 32-bit ARM boards like the NanoPi NEO Air: PyPI has no ready-made NumPy for them, so do this first there:

```
sudo apt install python3-venv python3-numpy
./install.sh
```

Data (zones, devices, event log, cached map tiles) goes in `sarsense-data` next to the scripts. Set `SARSENSE_DATA` to put it elsewhere.

### Offline install

For kits that will be set up with no internet, fill a `wheels` folder on a connected machine with the same operating system, processor type and Python version:

```
python -m pip download -d wheels -r requirements.txt
```

Both installers notice the folder and install from it without going online.

## Hub as a service

For a hub that starts on boot (Devuan, Debian, Armbian, Raspberry Pi OS):

```
sudo sh deploy/install_linux.sh
sudo nano /etc/sarsense/sarsense.json     # set "pin"
sudo service sarsense start               # or: systemctl start sarsense
```

This copies the hub to `/opt/sarsense`, uses the system Python and NumPy from apt, and installs the sysvinit script on systems without systemd or the unit file otherwise.

Every setting is also a command-line flag or a key in a JSON `--config` file: `./run_hub.sh --help`.

### Phones and location sharing

Browsers only share GPS with pages served over https. To let responders share location automatically:

```
sudo sh deploy/make_cert.sh 192.168.4.1
# then add "tls_cert" and "tls_key" to sarsense.json
```

Phones will warn about the self-signed certificate once. Without https, responders tap the map to set their position instead. Add the page to the home screen for an app-like icon.

## Sensor nodes (ESP32)

There are two versions of the node firmware. They behave the same and send the same packets, so you can mix them on one hub.

| | Arduino IDE | ESP-IDF |
|---|---|---|
| Folder | `firmware/arduino/SARSenseNode` | `firmware/esp32_csi_node` |
| Best for | Most people, quick setup | People already using ESP-IDF, custom builds |
| Wi-Fi settings | Top of the sketch, or typed into the Serial Monitor and saved on the board | `idf.py menuconfig` |
| Tested build | esp32 core 3.3.11: ESP32, S3, C6, C5 | ESP-IDF 5.4.1: ESP32, S3, C6, C5 |

**Arduino IDE:** install the *esp32 by Espressif Systems* boards package (3.0 or newer, 3.3 or newer for the C5), open `SARSenseNode.ino`, set your Wi-Fi at the top, upload. Full steps in `firmware/arduino/README.md`.

**ESP-IDF:**

```
cd firmware/esp32_csi_node
idf.py set-target esp32c6        # or esp32, esp32s3, esp32c5 ...
idf.py menuconfig                # SARSense node: Wi-Fi name, password, hub IP (blank = auto-find)
idf.py build flash monitor
```

The ESP32-C5 is a preview target in ESP-IDF 5.4 (`idf.py --preview set-target esp32c5`) and a normal one from 5.5. Neither firmware has been run on real boards yet, so treat the first flash as a test.

Each node joins the router, pings it about 20 times a second, and listens to every data frame on the channel. It forwards CSI from the router and from the other SARSense nodes the hub tells it about, capped at 25 reports per transmitter per second.

Router settings that matter:
- Use **20 MHz channel width**.
- On 2.4 GHz, disable 802.11b rates if the router allows it (OpenWrt does). The ESP32 only measures CSI on OFDM frames.
- All nodes on one router share that router's channel. One router plus its nodes is one sensing cell.

## Using it

The side panel (a pull-up sheet on phones) has five tabs. The tally at the top of the map shows the key numbers; tap one to jump to its tab, and tap anything on the map to find it in its tab.

| Tab | Holds |
|---|---|
| **Unknowns** | Everyone detected who is not a registered user: orange `U###` cards, still/breathing flags, lost contacts with last known position. Mark found, name, note or dismiss. |
| **Users** | Your own registration and location sharing, then every registered user with when their position was shared, whether stations can currently sense them, and which unknown they are standing with. |
| **Stations** | Sensors grouped under their router, plus other transmitters. Online, quiet or not responding; placed or not; links heard; signal to the router; firmware; address. Also calibration and the per-link signal view. |
| **Zones** | Draw and delete tagging and ignore zones. |
| **Hub** | Operator PIN, hub status, federation, activity log and CSV download. |

1. **Place stations.** Stations tab: *Place* each sensor and router, then tap where it is mounted. A link only helps locate people when both of its ends are placed.
2. **Calibrate.** With the area as empty and still as you can get it, press *Calibrate for 30 seconds* on the Stations tab.
3. **Draw zones.** Zones tab: a *tagging zone* around the search area; *ignore zones* over roads, fans, trees or your base.
4. **Register users.** Everyone helping opens the hub on their phone, Users tab, enters a name and shares location. Operators can also set a user's position by hand.
5. **Watch Unknowns.** A magenta ring means *still, possibly breathing*. Naming a missing person keeps them on the Unknowns tab with their ID, because they are still someone being searched for.

### How tagging decides

- Detections outside every tagging zone are shown as grey "other motion" (hidden by default).
- A new detection inside a tagging zone becomes a responder's track if a checked-in responder is within 15 m, otherwise a new `U###`.
- Unknown IDs stay unknown until an operator acts. If a responder reaches one, the card says "Sam is here" rather than hiding it.
- One exception, to stop responders spawning false unknowns: a brand-new unknown that has only ever been seen beside the same responder for its first 20 seconds is re-labelled as that responder, and the event log says so. Set `absorb_seconds` to 0 to turn this off.
- IDs are continuity labels. Wi-Fi sensing cannot tell who someone is, and two people who meet or cross can merge or swap.

## City scale: many hubs

Each hub covers the cells around it. Point sector hubs at a command hub (on Windows use `run_hub.bat` with the same options):

```
# command hub
./run_hub.sh --hub-id CMD --fed-key <secret> --pin <pin>
# sector hub
./run_hub.sh --hub-id N1 --fed-key <secret> --upstream http://<command-ip>:8080 --pin <pin>
```

Sector hubs push their tracks and placed devices every 2 seconds. Zones drawn on the command hub and all responder check-ins flow back down. Tracks from other hubs appear as `N1/U003`. Give every hub a different `--hub-id`. The link between hubs can be anything that carries HTTP: a mesh, a VPN, 4G.

## How detection works

**Per link** (one sensor hearing one transmitter), `sarsense/dsp.py`:
- *Movement score*: 1 minus the squared correlation between consecutive CSI amplitude frames, median over 2 s, turned into a z-score against a baseline learnt while the link is quiet.
- *Breathing*: after 24 s with no movement, the 8 most variable subcarriers are resampled to 10 Hz and searched for a 9 to 36 breaths/min peak.
- Each link locks onto one CSI frame format so mixed 11g/11n/11ax frames do not look like movement.

**Locating people**, `sarsense/tracker.py`:
- Each link paints an ellipse onto a 2 m grid. Busy links add evidence, quiet links subtract it.
- The hub takes the best point, removes the busy links that explain it, and repeats, so one person does not create ghosts where their links cross elsewhere.
- Detections must repeat for 3 ticks before becoming a track.

## Resource use

Measured on a desktop x86 CPU with 121 links at 20 Hz (about 2,400 packets a second): about 80 MB of memory and 11 % of one core. A NanoPi NEO Air's Cortex-A7 is several times slower, so start with `max_links` around 48 at 20 Hz (the default in `deploy/sarsense.json`) and raise it while watching `top`. Lower `SARS_TX_HZ` on the nodes if the hub falls behind. The unreadable-packets and refused-links counters on the Hub tab show when it is struggling.

## Tests

```
./install.sh --test                          # or: install.bat --test
.venv/bin/python tests/scene.py              # prints detections against ground truth
```

In the simulator: an empty area produces no tracks; a still person is found and flagged as breathing within about 4 m; two walkers plus a still person produce 4 to 5 tracks with a median position error of about 6 m inside a 60 m sensor ring. Real buildings will behave differently. Test in your own spaces before relying on it.

## Limits worth being honest about

- Reliable range is room to building scale per link, not streets.
- Stationary detection is fragile: fans, curtains, pets and trees cause false breathing flags; ignore zones and link muting help.
- Localisation is only as good as your device placement. Surround the search area; do not line sensors up.
- This is an aid for trained teams, not a replacement for search dogs, thermal imaging or voice contact.

## Files

```
sarsense/            hub (asyncio, NumPy)
  protocol.py        packet formats (ESP32 + Nexmon)
  dsp.py             per-link movement and breathing
  tracker.py         localisation, tracks, zones, responders
  hub.py             UDP intake, processing loop, federation
  web.py             HTTP API, live event stream, tile cache
  static/            web app (Leaflet and Atkinson Hyperlegible bundled)
firmware/            sensor node: arduino/ (Arduino IDE) and esp32_csi_node/ (ESP-IDF)
tools/               simulator, Nexmon forwarder, Windows shortcut helper
deploy/              sysvinit, systemd, installer, certificate helper
tests/               unit, API and scene tests
install.sh / .bat    create the local .venv and menu entries
run_hub.* run_demo.* start a hub, or a hub plus the simulator
```

Map tiles are OpenStreetMap, cached on the hub as they are viewed so they keep working offline. Please don't bulk-download OSM tiles; for large offline areas render your own and drop them into `<data_dir>/tiles/z/x/y.png`.

Licences: Leaflet (BSD-2-Clause) and Atkinson Hyperlegible (SIL OFL) are included with their licence files.
