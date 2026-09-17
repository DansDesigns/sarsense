# SARSense node for the Arduino IDE
Dedicated to Noah Woods & all missing children, I hope this helps bring you home.

Same behaviour and packet format as the ESP-IDF version, so both kinds of node can be mixed on one hub.

## Setup

1. In the Arduino IDE open **File > Preferences** and add this to *Additional boards manager URLs*:
   `https://espressif.github.io/arduino-esp32/package_esp32_index.json`
2. Open **Tools > Board > Boards Manager**, search for *esp32* and install **esp32 by Espressif Systems**, version 3.0 or newer (3.3 or newer for the ESP32-C5).
3. Open `SARSenseNode/SARSenseNode.ino`. Keep the folder name as it is; the IDE needs it to match the file name.
4. Edit the settings block at the top: Wi-Fi name, password, and the hub IP (leave it blank and the node finds the hub by itself).
5. Pick your board under **Tools > Board > esp32**. For ESP32-S3, C3, C5 and C6 boards that use the chip's own USB port, set **Tools > USB CDC On Boot > Enabled** so the Serial Monitor works.
6. Upload, then open the Serial Monitor at **115200 baud** with line ending **Newline**.

No extra libraries are needed.

## Changing settings without re-uploading

Type these into the Serial Monitor. They are saved on the board and take priority over the values in the sketch.

| Command | Does |
|---|---|
| `ssid <name>` | Wi-Fi network name |
| `pass <password>` | Wi-Fi password (leave blank for an open network) |
| `hub <ip>` | Hub address; `hub` on its own goes back to searching |
| `status` | Connection, hub and counters |
| `reset` | Forget serial settings |
| `restart` | Restart the node |

This means you can flash a batch of boards with the same sketch and set each one up on site.

## Which board

| Chip | Detects | Notes |
|---|---|---|
| ESP32-C5 | Movement and still people, 2.4 and 5 GHz | Best choice. Gets the same kind of CSI from every frame type |
| ESP32-C6 | Movement and still people, 2.4 GHz | Good, and common in cheap boards and smart relays |
| ESP32, S2, S3, C3 | Movement; still people only at short range | Fine for filling gaps |

A board with an external antenna connector picks up noticeably more than a PCB antenna.

## Compile check

With esp32 core 3.3.11 the sketch builds without warnings. Flash use: ESP32 69 %, S3 66 %, C6 75 %, C5 79 % of the default partition. It has not been run on real boards yet.
