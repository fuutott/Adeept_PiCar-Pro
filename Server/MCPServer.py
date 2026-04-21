#!/usr/bin/env/python
# File name   : MCPServer.py
# Website     : www.Adeept.com
# Author      : Adeept
# Date        : 2026/04/21
#
# Fourth entry point for the PiCar-Pro: an MCP server (Streamable HTTP) that
# owns the same hardware the WebServer/APPServer/GUIServer own. Mutually
# exclusive with the other three -- switch by editing ~/startup.sh.
#
# Layout mirrors WebServer.py: hardware subsystems are initialised at module
# import time, each in its own thread that blocks on a threading.Event, and
# MCP tools dispatch to them. Tool wrappers are async; blocking hardware
# calls that take more than a few ms (radar scan, frame grab, filmstrip
# encoding) are handed to anyio's thread pool so FastMCP's event loop stays
# responsive.

import asyncio
import json
import os
import re
import time

import anyio

import Move as move
import RPIservo
import Functions as functions
import RobotLight as robotLight
import Switch as switch
import Info as info
import Voltage
import Ultra as ultra
import Buzzer

from mcp.server.fastmcp import FastMCP, Image


Dv = -1  # Directional variable (same sign convention as WebServer.py)
OLED_connection = 1

try:
    import OLED
    screen = OLED.OLED_ctrl()
    screen.start()
    screen.screen_show(1, 'ADEEPT.COM')
    screen.screen_show(4, 'MCP MODE')
except Exception:
    OLED_connection = 0
    print('OLED disconnected')


# --- servos ---------------------------------------------------------------
scGear = RPIservo.ServoCtrl()
scGear.moveInit()
scGear.start()

init_pwm0 = scGear.initPos[0]
init_pwm1 = scGear.initPos[1]
init_pwm2 = scGear.initPos[2]
init_pwm3 = scGear.initPos[3]
init_pwm4 = scGear.initPos[4]

# --- motors ---------------------------------------------------------------
move.setup()

# --- composed autonomy ----------------------------------------------------
fuc = functions.Functions()
fuc.setup()
fuc.start()

# --- battery monitor ------------------------------------------------------
batteryMonitor = Voltage.BatteryLevelMonitor()
batteryMonitor.start()

# --- aux LEDs / turn signals ---------------------------------------------
switch.switchSetup()
switch.set_all_switch_off()

# --- WS2812 ring ----------------------------------------------------------
ws2812 = robotLight.Adeept_SPI_LedPixel(16, 255)
try:
    if ws2812.check_spi_state() != 0:
        ws2812.start()
        ws2812.breath(70, 70, 255)
except Exception:
    try:
        ws2812.led_close()
    except Exception:
        pass

# --- buzzer ---------------------------------------------------------------
player = Buzzer.Player()
player.start()

# --- camera + frame recorder ---------------------------------------------
# Importing app/camera_opencv spins up Picamera2 in a background thread.
from camera_opencv import Camera  # noqa: E402
from frame_recorder import FrameRecorder  # noqa: E402

camera = Camera()
recorder = FrameRecorder(camera, interval_s=0.5, max_frames=5)
recorder.start()

# Keep the MJPEG /video_feed endpoint up for debugging. Optional.
try:
    import app  # noqa: E402
    flask_app = app.webapp()
    flask_app.startthread()
except Exception as e:
    print(f'Flask video feed not started: {e}')


curpath = os.path.realpath(__file__)
thisPath = '/' + os.path.dirname(curpath)

# Locks -- one for motion (drive/turn) and one per servo group so two tools
# can't issue conflicting wiggles to the same servo simultaneously. stop()
# and emergency_stop() deliberately bypass these locks.
motion_lock = asyncio.Lock()
look_lock = asyncio.Lock()
arm_lock = asyncio.Lock()
hand_lock = asyncio.Lock()
grabber_lock = asyncio.Lock()


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _servo_ids_all():
    return [0, 1, 2, 3, 4]


def _servo_pwm_globals():
    return {
        0: init_pwm0,
        1: init_pwm1,
        2: init_pwm2,
        3: init_pwm3,
        4: init_pwm4,
    }


mcp = FastMCP('picar-pro', host='0.0.0.0', port=8765)


# ======================================================================
# Movement
# ======================================================================

@mcp.tool()
async def drive(direction: str, duration_s: float = 1.0, speed: int = 50) -> str:
    """Drive the car forward or backward for a bounded duration.

    direction: "forward" or "backward"
    duration_s: 0.1..3.0 seconds
    speed: 10..80 (percent throttle)
    """
    if direction not in ('forward', 'backward'):
        return f'error: direction must be "forward" or "backward", got {direction!r}'
    duration_s = _clamp(duration_s, 0.1, 3.0)
    speed = _clamp(int(speed), 10, 80)
    sign = 1 if direction == 'forward' else -1
    async with motion_lock:
        try:
            move.move(speed, sign, 'mid')
            await asyncio.sleep(duration_s)
        finally:
            move.motorStop()
    return f'drove {direction} for {duration_s:.2f}s at speed {speed}'


@mcp.tool()
async def turn(direction: str, duration_s: float = 0.5) -> str:
    """Pivot-style turn: deflects steer servo, drives briefly, re-centers.

    direction: "left" or "right"
    duration_s: 0.1..2.0 seconds
    """
    if direction not in ('left', 'right'):
        return f'error: direction must be "left" or "right", got {direction!r}'
    duration_s = _clamp(duration_s, 0.1, 2.0)
    angle = 30 * Dv if direction == 'left' else -30 * Dv
    signal_side = 3 if direction == 'left' else 2
    async with motion_lock:
        try:
            scGear.moveAngle(0, angle)
            await asyncio.sleep(0.15)
            move.move(30, 1, 'mid')
            switch.switch(signal_side, 1)
            await asyncio.sleep(duration_s)
        finally:
            move.motorStop()
            scGear.moveAngle(0, 0)
            switch.switch(2, 0)
            switch.switch(3, 0)
    return f'turned {direction} for {duration_s:.2f}s'


@mcp.tool()
async def stop() -> str:
    """Cut motor throttle and re-center steer. Bypasses locks."""
    move.motorStop()
    scGear.moveAngle(0, 0)
    return 'stopped'


# ======================================================================
# Servos (wiggle style -- matches WebServer.py's lookleft/armup/...)
# ======================================================================

async def _wiggle(lock, ch, direction_sign, speed, duration_s):
    duration_s = _clamp(duration_s, 0.05, 2.0)
    async with lock:
        try:
            scGear.singleServo(ch, direction_sign, speed)
            await asyncio.sleep(duration_s)
        finally:
            scGear.stopWiggle()


@mcp.tool()
async def look(direction: str, duration_s: float = 0.3) -> str:
    """Pan the camera. direction: "left" or "right"."""
    if direction == 'left':
        await _wiggle(look_lock, 1, 1, 7, duration_s)
    elif direction == 'right':
        await _wiggle(look_lock, 1, -1, 7, duration_s)
    else:
        return f'error: direction must be "left" or "right", got {direction!r}'
    return f'looked {direction} for {duration_s:.2f}s'


@mcp.tool()
async def arm(direction: str, duration_s: float = 0.3) -> str:
    """Raise or lower the arm. direction: "up" or "down"."""
    if direction == 'up':
        await _wiggle(arm_lock, 2, -1, 7, duration_s)
    elif direction == 'down':
        await _wiggle(arm_lock, 2, 1, 7, duration_s)
    else:
        return f'error: direction must be "up" or "down", got {direction!r}'
    return f'arm {direction} for {duration_s:.2f}s'


@mcp.tool()
async def hand(direction: str, duration_s: float = 0.3) -> str:
    """Rotate the hand (wrist). direction: "up" or "down"."""
    if direction == 'up':
        await _wiggle(hand_lock, 3, 1, 7, duration_s)
    elif direction == 'down':
        await _wiggle(hand_lock, 3, -1, 7, duration_s)
    else:
        return f'error: direction must be "up" or "down", got {direction!r}'
    return f'hand {direction} for {duration_s:.2f}s'


@mcp.tool()
async def grabber(action: str, duration_s: float = 0.3) -> str:
    """Operate the gripper. action: "grab" or "loose"."""
    if action == 'grab':
        await _wiggle(grabber_lock, 4, -1, 7, duration_s)
    elif action == 'loose':
        await _wiggle(grabber_lock, 4, 1, 7, duration_s)
    else:
        return f'error: action must be "grab" or "loose", got {action!r}'
    return f'grabber {action} for {duration_s:.2f}s'


@mcp.tool()
async def set_servo_angle(channel: int, angle_deg: int) -> str:
    """Move servo `channel` to `angle_deg` degrees from its trimmed center.

    angle_deg is the offset passed to moveAngle(); positive/negative values
    go in opposite directions depending on sc_direction[channel]. Clamped
    to +/-90 so we can't exceed the servo's physical range.
    """
    if channel not in _servo_ids_all():
        return f'error: channel must be 0..4, got {channel}'
    angle = _clamp(int(angle_deg), -90, 90)
    scGear.moveAngle(channel, angle)
    return f'servo {channel} moved to offset {angle} deg'


@mcp.tool()
async def home() -> str:
    """Move all 5 controlled servos (0..4) back to their trimmed centers."""
    scGear.moveServoInit(_servo_ids_all())
    return 'all servos homed'


# ======================================================================
# Sensors
# ======================================================================

@mcp.tool()
async def get_distance_cm() -> float:
    """One-shot ultrasonic ranging (HC-SR04). Returns cm."""
    return ultra.checkdist()


@mcp.tool()
async def get_line_sensors() -> dict:
    """Read the three IR line-tracking sensors. 1 = sees line."""
    return {
        'left': int(functions.track_line_left.value),
        'middle': int(functions.track_line_middle.value),
        'right': int(functions.track_line_right.value),
    }


@mcp.tool()
async def get_battery() -> dict:
    """Battery voltage, percent, and low-voltage warning flag."""
    voltage = float(Voltage.average_voltage)
    full = Voltage.full_voltage
    threshold = Voltage.WarningThreshold
    percentage = int((voltage - threshold) / (full - threshold) * 100) if voltage else 0
    return {
        'voltage': round(voltage, 2),
        'percentage': _clamp(percentage, 0, 100),
        'warning': bool(voltage and voltage < threshold),
    }


@mcp.tool()
async def get_status() -> dict:
    """CPU temp, CPU %, RAM %, battery %."""
    voltage = float(Voltage.average_voltage)
    full = Voltage.full_voltage
    threshold = Voltage.WarningThreshold
    battery_pct = int((voltage - threshold) / (full - threshold) * 100) if voltage else 0
    return {
        'cpu_temp_c': float(info.get_cpu_tempfunc()),
        'cpu_use_pct': float(info.get_cpu_use()),
        'ram_use_pct': float(info.get_ram_info()),
        'battery_pct': _clamp(battery_pct, 0, 100),
    }


@mcp.resource('robot://sensors/ultrasonic')
def resource_ultrasonic() -> str:
    return json.dumps({'distance_cm': ultra.checkdist()})


@mcp.resource('robot://sensors/line')
def resource_line() -> str:
    return json.dumps({
        'left': int(functions.track_line_left.value),
        'middle': int(functions.track_line_middle.value),
        'right': int(functions.track_line_right.value),
    })


@mcp.resource('robot://sensors/battery')
def resource_battery() -> str:
    voltage = float(Voltage.average_voltage)
    full = Voltage.full_voltage
    threshold = Voltage.WarningThreshold
    percentage = int((voltage - threshold) / (full - threshold) * 100) if voltage else 0
    return json.dumps({
        'voltage': round(voltage, 2),
        'percentage': _clamp(percentage, 0, 100),
        'warning': bool(voltage and voltage < threshold),
    })


@mcp.resource('robot://status/cpu')
def resource_cpu() -> str:
    return json.dumps({
        'cpu_temp_c': float(info.get_cpu_tempfunc()),
        'cpu_use_pct': float(info.get_cpu_use()),
        'ram_use_pct': float(info.get_ram_info()),
    })


# ======================================================================
# Camera
# ======================================================================

@mcp.tool()
async def capture_image() -> Image:
    """Grab a single live JPEG frame from the Pi camera."""
    frame = await anyio.to_thread.run_sync(camera.get_frame)
    if not frame:
        raise RuntimeError('camera returned no frame')
    return Image(data=frame, format='jpeg')


@mcp.tool()
async def capture_filmstrip() -> Image:
    """Concatenate the last 5 buffered frames (0.5s apart) left-to-right.

    Oldest frame on the left, newest on the right. If the recorder
    has fewer than 5 frames buffered (just started), only what's
    available is returned.
    """
    strip = await anyio.to_thread.run_sync(recorder.filmstrip_bytes)
    if not strip:
        raise RuntimeError('no frames buffered yet')
    return Image(data=strip, format='jpeg')


# ======================================================================
# Lights
# ======================================================================

@mcp.tool()
async def set_led_ring(
    mode: str,
    r: int = 0,
    g: int = 0,
    b: int = 0,
) -> str:
    """Set the WS2812 ring mode.

    mode: "off", "breath", "flowing", "rainbow", "police", "solid"
    r/g/b: 0..255, used by breath/flowing/rainbow/solid.
    """
    r = _clamp(int(r), 0, 255)
    g = _clamp(int(g), 0, 255)
    b = _clamp(int(b), 0, 255)
    if mode == 'off':
        ws2812.pause()
    elif mode == 'breath':
        ws2812.breath(r, g, b)
    elif mode == 'flowing':
        ws2812.flowing(r, g, b)
    elif mode == 'rainbow':
        ws2812.rainbow(r, g, b)
    elif mode == 'police':
        ws2812.police()
    elif mode == 'solid':
        ws2812.pause()
        ws2812.set_all_led_color(r, g, b)
    else:
        return f'error: unknown mode {mode!r}'
    return f'led ring: {mode}'


@mcp.tool()
async def set_turn_signal(side: str) -> str:
    """Turn signal LEDs. side: "left", "right", "off"."""
    if side == 'left':
        switch.switch(3, 1)
        switch.switch(2, 0)
    elif side == 'right':
        switch.switch(2, 1)
        switch.switch(3, 0)
    elif side == 'off':
        switch.switch(2, 0)
        switch.switch(3, 0)
    else:
        return f'error: side must be "left", "right", or "off", got {side!r}'
    return f'turn signal: {side}'


@mcp.tool()
async def set_aux_led(channel: int, on: bool) -> str:
    """Raw aux-LED toggle. channel: 1, 2, or 3."""
    if channel not in (1, 2, 3):
        return f'error: channel must be 1, 2, or 3, got {channel}'
    switch.switch(channel, 1 if on else 0)
    return f'aux led {channel}: {"on" if on else "off"}'


# ======================================================================
# OLED
# ======================================================================

@mcp.tool()
async def display_text(row: int, text: str) -> str:
    """Write to one of the 6 OLED rows. row: 1..6. text trimmed to 20 chars."""
    if not 1 <= row <= 6:
        return f'error: row must be 1..6, got {row}'
    if not OLED_connection:
        return 'error: OLED disconnected'
    text = text[:20]
    screen.screen_show(row, text)
    return f'row {row}: {text!r}'


# ======================================================================
# Buzzer
# ======================================================================

@mcp.tool()
async def play_happy_birthday() -> str:
    """Play the hard-coded Happy Birthday tune via the passive buzzer."""
    player.start_playing()
    return 'playing'


@mcp.tool()
async def stop_music() -> str:
    """Stop whatever the buzzer is currently playing."""
    player.pause()
    return 'stopped'


@mcp.tool()
async def beep(note: str = 'C5', duration_s: float = 0.2) -> str:
    """Play a single note. note: e.g. "C4", "A5"."""
    duration_s = _clamp(duration_s, 0.05, 2.0)
    try:
        Buzzer.tb.play(note)
        await asyncio.sleep(duration_s)
    finally:
        Buzzer.tb.stop()
    return f'beep {note} {duration_s:.2f}s'


# ======================================================================
# Servo trim / calibration
# ======================================================================

@mcp.tool()
async def get_trims() -> dict:
    """Read the current in-memory trim offsets (init_pwm0..4)."""
    return {ch: val for ch, val in _servo_pwm_globals().items()}


@mcp.tool()
async def nudge_trim(channel: int, direction: str, steps: int = 1) -> dict:
    """Shift trim for `channel` by +/-2*steps (direction "left" or "right").

    In-memory only. Call persist_trims() to save across reboots.
    """
    global init_pwm0, init_pwm1, init_pwm2, init_pwm3, init_pwm4
    if channel not in _servo_ids_all():
        return {'error': f'channel must be 0..4, got {channel}'}
    if direction not in ('left', 'right'):
        return {'error': f'direction must be "left" or "right", got {direction!r}'}
    steps = _clamp(int(steps), 1, 20)
    delta = -2 * steps if direction == 'left' else 2 * steps
    if channel == 0:
        init_pwm0 = _clamp(init_pwm0 + delta, 0, 180); new_val = init_pwm0
    elif channel == 1:
        init_pwm1 = _clamp(init_pwm1 + delta, 0, 180); new_val = init_pwm1
    elif channel == 2:
        init_pwm2 = _clamp(init_pwm2 + delta, 0, 180); new_val = init_pwm2
    elif channel == 3:
        init_pwm3 = _clamp(init_pwm3 + delta, 0, 180); new_val = init_pwm3
    else:
        init_pwm4 = _clamp(init_pwm4 + delta, 0, 180); new_val = init_pwm4
    scGear.setPWM(channel, new_val)
    return {'channel': channel, 'value': new_val, 'persisted': False}


@mcp.tool()
async def reset_trims() -> dict:
    """Reset all trims to 90 and move all servos to center. In-memory only."""
    global init_pwm0, init_pwm1, init_pwm2, init_pwm3, init_pwm4
    init_pwm0 = init_pwm1 = init_pwm2 = init_pwm3 = init_pwm4 = 90
    for i in _servo_ids_all():
        scGear.moveAngle(i, 0)
    return {'trims': _servo_pwm_globals(), 'persisted': False}


def _persist_one(initial_prefix, new_value):
    # Rewrites a single `init_pwm<N> = <int>` line in RPIservo.py.
    # Preserves every other line byte-for-byte.
    target = thisPath + '/RPIservo.py'
    pattern = re.compile(r'^' + re.escape(initial_prefix) + r'\d+\s*$')
    replacement = f'{initial_prefix}{new_value}\n'
    matched = False
    with open(target, 'r') as f:
        lines = f.readlines()
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = replacement
            matched = True
            break
    if not matched:
        raise RuntimeError(
            f'could not find line starting with {initial_prefix!r} in {target}'
        )
    with open(target, 'w') as f:
        f.writelines(lines)


@mcp.tool()
async def persist_trims() -> dict:
    """Write the current in-memory trims back into Server/RPIservo.py.

    This is the two-step split: nudge_trim() changes values in memory;
    persist_trims() commits them so they survive a reboot. Preserves the
    CLAUDE.md invariant that `init_pwm<N> = <int>` lives on one line
    at module top-level with no inline comments.
    """
    pairs = [
        ('init_pwm0 = ', init_pwm0),
        ('init_pwm1 = ', init_pwm1),
        ('init_pwm2 = ', init_pwm2),
        ('init_pwm3 = ', init_pwm3),
        ('init_pwm4 = ', init_pwm4),
    ]
    try:
        for prefix, value in pairs:
            _persist_one(prefix, value)
    except Exception as e:
        return {'error': str(e), 'persisted': False}
    return {'trims': _servo_pwm_globals(), 'persisted': True}


# ======================================================================
# Composed autonomy (reuses Server/Functions.py)
# ======================================================================

@mcp.tool()
async def radar_scan() -> list:
    """Sweep the pan servo and collect ultrasonic hits.

    Returns a list of [distance_cm, theta_deg] pairs for obstacles within
    50 cm. Blocks ~2 seconds on the hardware.
    """
    return await anyio.to_thread.run_sync(fuc.radarScan)


@mcp.tool()
async def auto_obstacle_avoid(enable: bool) -> str:
    """Toggle the autonomous obstacle-avoidance behaviour."""
    if enable:
        fuc.automatic()
        return 'auto_obstacle_avoid: on'
    fuc.pause()
    move.motorStop()
    return 'auto_obstacle_avoid: off'


@mcp.tool()
async def track_line(enable: bool) -> str:
    """Toggle IR line-following (GPIO 17/22/27)."""
    if enable:
        functions.last_status = None
        fuc.trackLine()
        return 'track_line: on'
    fuc.pause()
    move.motorStop()
    return 'track_line: off'


@mcp.tool()
async def keep_distance(enable: bool) -> str:
    """Toggle follow-at-fixed-distance (uses ultrasonic)."""
    if enable:
        functions.last_status = 25
        fuc.keepDistance()
        return 'keep_distance: on'
    fuc.pause()
    move.motorStop()
    return 'keep_distance: off'


@mcp.tool()
async def steady_camera(enable: bool) -> str:
    """Toggle camera-steadying (compensates chassis motion on tilt)."""
    if enable:
        fuc.steady(scGear.lastPos[2])
        return 'steady_camera: on'
    fuc.pause()
    return 'steady_camera: off'


@mcp.tool()
async def emergency_stop() -> str:
    """Hard stop: motors off, wiggle off, autonomy off, turn signals off."""
    move.motorStop()
    scGear.stopWiggle()
    fuc.pause()
    switch.switch(2, 0)
    switch.switch(3, 0)
    return 'emergency_stop'


# ======================================================================
# Entry point
# ======================================================================

if __name__ == '__main__':
    if OLED_connection:
        try:
            screen.screen_show(5, 'MCP :8765')
        except Exception:
            pass
    print('MCP server listening on http://0.0.0.0:8765/mcp')
    try:
        mcp.run(transport='streamable-http')
    except KeyboardInterrupt:
        pass
    finally:
        try:
            recorder.stop()
        except Exception:
            pass
        try:
            move.destroy()
        except Exception:
            pass
