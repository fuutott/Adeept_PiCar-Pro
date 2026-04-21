# CLAUDE.md

Guidance for AI assistants (Claude Code, etc.) working in this repository.

## Project overview

This repo is the official software stack for the **Adeept PiCar-Pro** (item code ADR019), an educational Raspberry Pi robot car with a 4-DOF robotic arm, pan/tilt camera, WS2812 LED ring, ultrasonic sensor, line-tracking sensors, OLED screen, microphone, and buzzer. Compatible with Raspberry Pi 3B, 3B+, 4, and 5.

The code is Python 3 running on Raspberry Pi OS (Bullseye / Bookworm / Trixie). There is no build system, no test suite, and no linter configured — it is a fleet of independently executable scripts that talk to GPIO, I2C (PCA9685 servo/motor driver at `0x5f`, ADS7830 ADC at `0x48`, SSD1306 OLED at `0x3C`), SPI (WS2812 LEDs), and the Pi camera.

## Repository layout

```
Adeept_PiCar-Pro/
├── README.md               # Product blurb (not engineering docs)
├── setup.py                # One-shot installer (NOT Python packaging); run as root
├── initPosServos.py        # Centers all 16 PCA9685 servo channels to 90° for assembly
├── wifi_hotspot_manager.sh # systemd-installed script: join Wi-Fi or fall back to AP
├── Server/                 # Runs on the Pi (robot-side)
├── Client/                 # Tkinter desktop control app (runs on PC)
└── Examples/               # Standalone learning demos, one subdir per peripheral
```

### `Server/` — robot-side application

Three mutually-exclusive entry points live here. Exactly one is run by the systemd autostart unit (default: `WebServer.py`, set in `setup.py`).

| Entry point | Purpose | Control protocol |
|---|---|---|
| `WebServer.py`   | Default. Serves the Vue/webpack SPA in `Server/dist/` on port 5000 and a JSON-over-WebSocket control channel on port **8888**. | WebSocket (ws://pi:8888) |
| `APPServer.py`   | Same architecture as WebServer but tuned for the Adeept mobile app (different message formats, RGB color space for `findColor`, speed `*10`, no auth). | WebSocket 8888 |
| `GUIServer.py`   | TCP socket server for the Tkinter `Client/GUI.py`; streams video via ZMQ (`Server/FPV.py`). | Raw TCP 10223 + ZMQ 5555 |

Only one of these should be running at a time — they all bind the same motor/servo/camera hardware.

Shared modules (imported by the entry points):

- `app.py` — Flask app that serves the `dist/` SPA and the MJPEG `/video_feed` endpoint. Always imported by `WebServer.py`/`APPServer.py`.
- `camera_opencv.py` — `Camera` (Picamera2 → OpenCV frames) and `CVThread` (findColor / watchDog / findlineCV / object tracking). Relies on `base_camera.py`.
- `base_camera.py` — Generic single-producer/multi-consumer camera frame broker (per-thread `CameraEvent`).
- `FPV.py` — Used by `GUIServer.py` only; ZMQ publisher pushing JPEG frames to the Tkinter client plus on-stream OSD.
- `Move.py` — DC motor control via PCA9685 (channels 8–15). Exposes `setup()`, `move(speed, direction, turn)`, `motorStop()`, `destroy()`. **Motor direction constants `M1_Direction`/`M2_Direction`** are here; flip their sign if a motor wires up backward.
- `RPIservo.py` — `ServoCtrl(threading.Thread)`: 8-channel servo controller with `moveInit`, `moveAngle`, `singleServo` (wiggle), `setPWM`, `stopWiggle`. **Runtime state is persisted by rewriting lines like `init_pwm0 = 90` in this file** via `WebServer.replace_num` / `Functions.num_import_int` — when editing `RPIservo.py`, preserve the `init_pwm<N> = <int>` lines at top-level, one per line, no inline comments.
- `Functions.py` — Autonomous behaviors: radar scan (servo sweep + ultrasonic), obstacle avoidance (`automatic`), line tracking via GPIO (`trackLine`, pins 17/22/27), keep-distance, camera steady. All run inside `Functions(threading.Thread)`.
- `RobotLight.py` — `Adeept_SPI_LedPixel` WS2812 driver over SPI (breathing, flowing, rainbow, police modes).
- `Switch.py` — `LED` wrappers on GPIO 9 / 11 / 25 ("switches" 1/2/3, also used as left/right turn signals).
- `Ultra.py` — `gpiozero.DistanceSensor` on GPIO 23 (trigger) / 24 (echo).
- `OLED.py` — 128×64 SSD1306 over I2C address `0x3C`; 6-row display buffer updated by `screen_show(position, text)`.
- `Voltage.py` — `BatteryLevelMonitor(threading.Thread)` reading ADS7830 ch0 (R15=3k, R17=1k divider). Warns + beeps when below 6.3 V of the 8.4 V full scale.
- `Buzzer.py` — `Player(threading.Thread)` that plays a hard-coded "Happy Birthday" tune on `TonalBuzzer(18)`.
- `Voice_Command.py` — Two threads: `Sherpa_ncnn` shells out to the `sherpa-ncnn-alsa` binary (hard-coded path `/home/pi/sherpa-ncnn/…`), piping recognition output to `output.txt`; `Speech` tails that file and maps keywords (`lookleft`, `armup`, `grab`, …) to `scGear.singleServo` calls.
- `VoiceIdentify.py` — Just the `sherpa-ncnn-alsa` invocation, spawned as its own `sudo python` subprocess.
- `Info.py` — CPU temp / CPU usage / RAM% helpers via `psutil` and `/sys/class/thermal`.
- `Kalman_Filter.py`, `PID.py` — Small helpers used by the CV tracking loop.
- `dist/` — Prebuilt Vue SPA (index.html + JS/CSS/fonts). Served by `app.py`. **Treat as a binary artifact** — do not edit by hand.

### `Client/` — desktop control app

- `GUI.py` — Tkinter control panel. Opens a TCP socket to the Pi on port 10223, sends single-word commands (`forward`, `lookleft`, `scan`, `PWMD`, …), receives JSON status (`get_info`, `scanResult`, switch/function state). It also spawns `Footage-GUI.py` to display the ZMQ video stream.
- `Footage-GUI.py` — Minimal ZMQ `PAIR` server on port 5555 that receives base64-JPEG frames from `FPV.py` and renders them with OpenCV.
- `logo.png` — UI asset.

### `Examples/` — peripheral demos

15 numbered folders, each a small standalone script teaching one subsystem (LED, buzzer, servo, motor, WS2812, ultrasonic, line tracking, OLED, Pi camera Flask server, OpenCV findColor/gesture/watchDog, voltage, MPU6050, microphone + sherpa-ncnn speech, DeepSeek chat, a trivial TCP remote). These are pedagogical — they duplicate logic from `Server/` rather than importing it. Prefer editing `Server/` for production-path changes.

Notable: `Examples/14_Example_Of_AI/TalkToAI.py` contains a **hard-coded DeepSeek API key** in the committed file. Leave it alone unless the user explicitly requests a rotation/removal; flag it if you notice it drifting.

## Installation & deployment

`setup.py` is **not** a setuptools file — it is a shell-like installer meant to be run once on the Pi as root:

```bash
sudo python3 setup.py
```

It: updates apt, installs system packages (`python3-picamera2`, `python3-opencv`, `python3-pyaudio`, `i2c-tools`, `python3-gpiozero`, …), pip-installs CircuitPython drivers for PCA9685 / motor / ADS7830 / SSD1306 plus Flask, websockets==13.0, pyzmq, imutils, pillow, numpy, and **reboots**. On Debian 12+ (Bookworm/Trixie) it uses `--break-system-packages`.

It also installs two systemd units:

- `wifi-hotspot-manager.service` — runs `~/<user>/wifi_hotspot_manager.sh` once at boot; tries the preconfigured Wi-Fi, else brings up an AP named `Adeept_Robot` (password `12345678`, gateway `192.168.4.1`).
- `Adeept_Robot.service` — runs `~/<user>/startup.sh` which itself runs `sudo python3 <repo>/Server/WebServer.py` after a 5-second delay. To switch to `APPServer.py` or `GUIServer.py`, edit `~/startup.sh`.

`initPosServos.py` is a standalone helper: import Adafruit PCA9685, drive all 16 channels to 90°, and hold. Run it when mechanically assembling the arm/pan-tilt so servos are at their neutral position.

## Running manually (development)

All robot-side scripts must run on the Pi with root (GPIO/I2C/SPI need it):

```bash
# Stop the autostart service first so the hardware isn't double-claimed
sudo systemctl stop Adeept_Robot.service

# Then run one of:
sudo python3 Server/WebServer.py   # default (web UI + ws)
sudo python3 Server/APPServer.py   # mobile app
sudo python3 Server/GUIServer.py   # Tkinter client

# Individual module self-tests (each has an `if __name__ == '__main__'` block):
sudo python3 Server/Move.py
sudo python3 Server/RPIservo.py
sudo python3 Server/Ultra.py
sudo python3 Server/Switch.py
# ... etc.
```

From a PC on the same network:

```bash
python3 Client/GUI.py           # needs GUIServer.py running on the Pi
```

## Hardware / pin map (for sanity checking changes)

| Peripheral | Interface | Address / pins |
|---|---|---|
| Servo driver (PCA9685) | I2C | `0x5f` — 16 channels; ch0–4 used (chassis steering, pan, tilt, hand, grab) |
| Motor driver (same PCA9685) | I2C `0x5f` | M1=ch15/14, M2=ch12/13, M3=ch11/10, M4=ch8/9 |
| ADS7830 battery ADC | I2C | `0x48`, channel 0 |
| OLED SSD1306 | I2C | `0x3C` |
| WS2812 LED ring (16 px) | SPI0 MOSI | GPIO 10 |
| Ultrasonic HC-SR04 | GPIO | Trigger 23, Echo 24 |
| Line-tracking IR | GPIO | Left 22, Middle 27, Right 17 |
| Turn-signal / aux LEDs | GPIO | Switch1=9, Switch2=25, Switch3=11 |
| Passive buzzer | PWM | GPIO 18 |
| Network | — | WebSocket 8888, HTTP/MJPEG 5000, GUIServer TCP 10223, ZMQ video 5555 |

The `WebServer.py` WebSocket accepts auth `admin:123456`; `APPServer.py` skips auth. Don't change these without explicit direction — the prebuilt SPA in `dist/` and the mobile app hard-code them.

## Coding conventions observed

- **Python 3, tabs + spaces inconsistently** — match the surrounding file. Don't reformat wholesale.
- Shebangs are whimsical (`#!/usr/bin/env/python` — note the trailing `/python`). Leave them alone.
- Every file carries a header: `File name`, `Website`, `Author`, `Date`. Preserve it.
- Long-running subsystems are `threading.Thread` subclasses with a `threading.Event` flag: `pause()` clears, `resume()` sets, `run()` blocks on `self.__flag.wait()`. Follow the same pattern for new background workers.
- Global module state (e.g. `init_pwm0`, `modeSelect`, `colorUpper`) is the norm. Mutation happens via top-level functions or `global` declarations inside methods.
- Errors around optional hardware (OLED, LEDs, camera) are deliberately swallowed with bare `try/except: pass` so the robot still boots when accessories are disconnected — keep that pattern.
- Protocol-level string commands (`forward`, `SiLeft2`, `PWMINIT`, `CVFLL1 40`, `Switch_1_on`) are duck-typed with `in` / `==` checks across `robotCtrl` / `switchCtrl` / `functionSelect` / `configPWM`. Adding a new command means editing the matching dispatcher in **all** of `WebServer.py`, `APPServer.py`, and `GUIServer.py` if you want parity — otherwise call out which front-ends you're (not) updating.
- `RPIservo.py` is rewritten at runtime to persist servo trim values. Don't put logic at module top-level that would break `open().readlines()` parsing for `init_pwm<N> = <int>`.

## What not to touch without explicit direction

- `Server/dist/` — prebuilt SPA; no source in this repo.
- `Examples/14_Example_Of_AI/TalkToAI.py` — committed DeepSeek API key.
- Systemd unit content in `setup.py` — running it rewrites `/etc/systemd/system/*.service` and triggers a reboot.
- Hard-coded paths `/home/pi/sherpa-ncnn/...` and `/home/pi/sherpa-ncnn-streaming-zipformer-bilingual-zh-en-2023-02-13/...` in `VoiceIdentify.py` / `Voice_Command.py` — they assume a specific non-repo model download.
- `M1_Direction` / `M2_Direction` in `Move.py` and `Dv = -1` sprinkled in several files — these are per-build mechanical sign flips; change them only when the user says a motor or servo moves the wrong way.

## Git / branch workflow

- Default development branch for AI assistants: **`claude/add-claude-documentation-lJUe3`** (see task instructions).
- Upstream history is predominantly "Add files via upload" — upstream commits from the vendor are full-directory drops, not incremental changes. Our commits should be conventional and descriptive.
- No CI is configured. There is no test runner; verification means either hardware-in-the-loop on a Pi or code review.

## Quick orientation checklist for new tasks

1. Confirm whether the change is robot-side (`Server/`), client-side (`Client/`), or a standalone demo (`Examples/`). Cross-file duplication is deliberate — don't refactor across those boundaries.
2. If adding a new websocket/TCP command, decide which of `WebServer.py` / `APPServer.py` / `GUIServer.py` need it, and note the ones you're skipping.
3. If editing a module that maintains state in `RPIservo.py`'s top-level `init_pwm<N> = <int>` lines, preserve the exact format.
4. If touching hardware pin assignments, update the Hardware table above.
5. Test hooks: run the relevant file's `__main__` self-test on real hardware; there is no CI/unit-test equivalent.
