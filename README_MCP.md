# MCP Server for PiCar-Pro

`Server/MCPServer.py` is a fourth entry point for the robot (alongside
`WebServer.py`, `APPServer.py`, `GUIServer.py`) that exposes the hardware
over [Model Context Protocol](https://modelcontextprotocol.io) using
Streamable HTTP. It lets an MCP client such as Claude Code drive the car
directly as a set of tools.

The MCP server binds the same PCA9685, GPIO, and camera hardware as the
other three entry points, so **only one of the four can run at a time**.

## Requirements

- Raspberry Pi OS Bookworm or Trixie (Python 3.10+).
  Bullseye / Python 3.9 is not supported — `mcp>=1.6` requires 3.10.
- Everything `setup.py` installs, plus the extras in
  [`mcp_requirements.txt`](mcp_requirements.txt):
  - `mcp[cli]>=1.6.0`
  - `anyio>=4.0`

## Install

On the Pi, from the repo root:

```bash
# If setup.py was run previously, the rest of the stack is already installed.
# Install just the MCP extras:
sudo pip3 install -r mcp_requirements.txt --break-system-packages
```

(On Bullseye/Python 3.9 the install will fail the version gate — intended.)

## Run

Stop whichever of the three existing servers is running:

```bash
sudo systemctl stop Adeept_Robot.service
```

Then launch the MCP server (needs root for GPIO/I2C/SPI):

```bash
sudo python3 Server/MCPServer.py
```

It binds `0.0.0.0:8765` — the MCP endpoint is `http://<pi-ip>:8765/mcp`.

The MJPEG video feed at `http://<pi-ip>:5000/video_feed` stays up for
debugging; the Vue SPA page itself loads but its control buttons are
no-ops because `WebServer.py`'s WebSocket on :8888 isn't bound.

## Autostart on boot

The installed systemd unit `Adeept_Robot.service` runs `~/startup.sh`,
which by default starts `Server/WebServer.py`. To switch to MCP, edit
`~/startup.sh` on the Pi and replace the `WebServer.py` line with:

```sh
sudo python3 /<path-to-repo>/Server/MCPServer.py
```

## Connect Claude Code

On your laptop, add the server to `.mcp.json` at the project root or to
your user Claude config:

```json
{
  "mcpServers": {
    "picar": {
      "type": "http",
      "url": "http://<pi-ip>:8765/mcp"
    }
  }
}
```

Launch Claude Code in the project; `picar` tools will appear.

## Available tools

| Category | Tools |
|---|---|
| Movement | `drive`, `turn`, `stop` |
| Servos (wiggle) | `look`, `arm`, `hand`, `grabber` |
| Servos (absolute) | `set_servo_angle`, `home` |
| Trim / calibration | `get_trims`, `nudge_trim`, `reset_trims`, `persist_trims` |
| Sensors | `get_distance_cm`, `get_line_sensors`, `get_battery`, `get_status` |
| Camera | `capture_image`, `capture_filmstrip` |
| Lights | `set_led_ring`, `set_turn_signal`, `set_aux_led` |
| OLED | `display_text` |
| Buzzer | `play_happy_birthday`, `stop_music`, `beep` |
| Autonomy | `radar_scan`, `auto_obstacle_avoid`, `track_line`, `keep_distance`, `steady_camera`, `emergency_stop` |

## MCP resources

Live sensor reads exposed as subscribable resources:

- `robot://sensors/ultrasonic`
- `robot://sensors/line`
- `robot://sensors/battery`
- `robot://status/cpu`

## Calibration workflow

Servo trim offsets live as `init_pwm0..4` at the top of
`Server/RPIservo.py` (default 90 each). Nudging is a two-step process —
`nudge_trim` changes the value in memory, `persist_trims` rewrites
`RPIservo.py` so the change survives a reboot:

```text
get_trims()                       -> {0: 90, 1: 90, ...}
nudge_trim(0, "left", 2)          -> {channel: 0, value: 86, persisted: False}
get_trims()                       -> {0: 86, 1: 90, ...}
persist_trims()                   -> {trims: {0: 86, ...}, persisted: True}
# RPIservo.py now shows `init_pwm0 = 86` at the top of the file.
```

`reset_trims()` resets everything to 90 in memory; it does **not**
auto-persist — call `persist_trims()` if you also want that written.

## Safety notes

- `drive` and `turn` both implement a dead-man timer: the motor stop runs
  inside a `finally` block so a disconnected MCP client halts the car
  within ~100 ms instead of running until the duration expires.
- `drive` is clamped to 10..80 speed and 0.1..3.0 s; `turn` to 0.1..2.0 s.
- `set_servo_angle` is clamped to +/-90 degrees per channel.
- `stop` and `emergency_stop` bypass per-subsystem locks — they always win.
- **No MCP-level auth.** The Pi is assumed to be on a trusted LAN (your
  Wi-Fi or the robot's own `Adeept_Robot` / `12345678` hotspot). Do not
  expose port 8765 to the public internet.

## Out of scope (v1)

CV modes (`findColor`, `watchDog`, `findlineCV`) and voice-command
recognition (sherpa-ncnn) are not exposed by the MCP server yet. They
continue to work under `WebServer.py` and `APPServer.py`.
