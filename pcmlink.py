#!/usr/bin/env python3
"""pcmlink - uncompressed PCM audio over LAN.

Captures system audio on one machine and plays it on another machine's audio
interface, as RTP/L16 over UDP. No compression, no discovery, no GUI.

Receiving needs the GStreamer Python bindings (for live statistics).
Sending falls back to invoking gst-launch-1.0 when they are unavailable,
which is the normal situation on Windows.
"""
from __future__ import annotations

import argparse
import os
import platform
import shlex
import shutil
import socket
import subprocess
import sys
import time

APP = "pcmlink"
CONFIG_BASENAME = "config.toml"
PROJECT_BASENAME = "pcmlink.toml"

# Every setting, its default, and the help text used by both --help and the
# generated example config. Keeping one table avoids the usual drift between
# flags, config keys and documentation.
SETTINGS: dict[str, tuple[object, str]] = {
    "host":         ("",       "receiver address (sender only)"),
    "bind":         ("0.0.0.0","local address to listen on (receiver only)"),
    "port":         (5004,     "UDP port"),
    "rate":         (48000,    "sample rate, must match on both ends"),
    "channels":     (2,        "channel count"),
    "payload":      (96,       "RTP dynamic payload type"),
    "mtu":          (1200,     "RTP payload MTU in bytes (sender only)"),
    "latency":      (100,      "jitterbuffer depth in ms (receiver only)"),
    "buffer_time":  (100000,   "output device buffer in microseconds"),
    "latency_time": (20000,    "output device period in microseconds"),
    "device":       ("",       "device name substring; empty means system default"),
    "format":       ("F32LE",  "sample format handed to the output device"),
    "volume":       (1.0,      "software gain, 1.0 = unity, 0.5 = -6 dB, 2.0 = +6 dB"),
    "stats":        (True,     "print one statistics line per second"),
    "tone_freq":    (440,      "test tone frequency in Hz"),
    "tone_volume":  (0.05,     "test tone volume, 0.0 to 1.0"),
    "gst_launch":   ("",       "path to gst-launch-1.0 (blank = search PATH)"),
    "retry":        (True,     "rebuild and restart the pipeline after a failure"),
    "retry_delay":  (5,        "seconds to wait before retrying"),
}
DEFAULTS = {k: v for k, (v, _) in SETTINGS.items()}


def die(msg: str, code: int = 1):
    print(f"{APP}: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- config
# Layered, least important first. Follows the XDG Base Directory Specification
# on Unix: "The base directory defined by $XDG_CONFIG_HOME is considered more
# important than any of the base directories defined by $XDG_CONFIG_DIRS", and
# "when the same information is defined in multiple places the information
# defined relative to the more important base directory takes precedent."
# Windows uses %PROGRAMDATA% then %APPDATA%, its conventional equivalents.

def config_paths() -> list[str]:
    paths: list[str] = []
    if platform.system() == "Windows":
        for env in ("PROGRAMDATA", "APPDATA"):
            base = os.environ.get(env)
            if base:
                paths.append(os.path.join(base, APP, CONFIG_BASENAME))
    else:
        dirs = os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg"
        for d in reversed([p for p in dirs.split(os.pathsep) if p]):
            paths.append(os.path.join(d, APP, CONFIG_BASENAME))
        home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        paths.append(os.path.join(home, APP, CONFIG_BASENAME))
        if platform.system() == "Darwin":
            paths.append(os.path.expanduser(
                f"~/Library/Application Support/{APP}/{CONFIG_BASENAME}"))
    paths.append(os.path.join(os.getcwd(), PROJECT_BASENAME))
    return paths


def load_config(explicit: str | None) -> tuple[dict, list[str]]:
    import tomllib
    if explicit:
        if not os.path.isfile(explicit):
            die(f"config file not found: {explicit}")
        with open(explicit, "rb") as fh:
            return tomllib.load(fh), [explicit]
    merged: dict = {}
    used: list[str] = []
    for path in config_paths():
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            try:
                data = tomllib.load(fh)
            except Exception as exc:
                die(f"{path}: {exc}")
        for key, val in data.items():
            if isinstance(val, dict):
                merged.setdefault(key, {}).update(val)
            else:
                merged[key] = val
        used.append(path)
    return merged, used


def coerce(default, raw):
    if isinstance(default, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return type(default)(raw)


def resolve(args, cfg: dict, role: str) -> dict:
    """defaults < config top level < config [role] < environment < CLI flags."""
    role_tbl = cfg.get(role) if isinstance(cfg.get(role), dict) else {}
    out = {}
    for key, default in DEFAULTS.items():
        val = default
        if key in cfg and not isinstance(cfg[key], dict):
            val = cfg[key]
        if role_tbl and key in role_tbl:
            val = role_tbl[key]
        env = os.environ.get(f"{APP.upper()}_{key.upper()}")
        if env is not None:
            val = coerce(default, env)
        cli = getattr(args, key, None)
        if cli is not None:
            val = cli
        out[key] = val
    return out


# ---------------------------------------------------------------- gstreamer

def gst(required: bool = True):
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst, GLib
    except Exception as exc:
        if not required:
            return None, None
        die(f"GStreamer Python bindings unavailable: {exc}\n"
            f"  macOS:  brew install gstreamer pygobject3\n"
            f"  Debian: apt install python3-gi gstreamer1.0-plugins-base "
            f"gstreamer1.0-plugins-good gstreamer1.0-plugins-bad")
    if not Gst.is_initialized():
        Gst.init(None)
    return Gst, GLib


def find_gst_launch(cfg: dict) -> str | None:
    if cfg["gst_launch"]:
        return cfg["gst_launch"] if os.path.isfile(cfg["gst_launch"]) else None
    found = shutil.which("gst-launch-1.0")
    if found:
        return found
    if platform.system() == "Windows":
        guess = r"C:\Program Files\gstreamer\1.0\msvc_x86_64\bin\gst-launch-1.0.exe"
        if os.path.isfile(guess):
            return guess
    return None


def source_element(cfg: dict, tone: bool) -> str:
    if tone:
        return (f"audiotestsrc is-live=true wave=sine "
                f"freq={cfg['tone_freq']} volume={cfg['tone_volume']}")
    system = platform.system()
    if system == "Windows":
        # WASAPI loopback captures an existing render endpoint, so Windows needs
        # no virtual sound card. low-latency is deliberately left off: it starves
        # capture and produces continuous clicking.
        return "wasapi2src loopback=true"
    if system == "Linux":
        # A PipeWire/PulseAudio sink monitor is an ordinary capture source, and
        # pulsesrc with no device follows the default sink's monitor.
        return "pulsesrc"
    die("no default system-audio capture on this platform: pass --device naming "
        "a capture source, such as a loopback device like BlackHole")


def device_launch_fragment(dev) -> str:
    """Render a Gst.Device as 'factory prop="value"' for a launch string.

    Gst.Device.create_element() configures the element correctly on every
    platform; we read the settings back rather than guessing which property
    each platform's element uses (unique-id on macOS, device elsewhere).
    """
    el = dev.create_element(None)
    if el is None:
        die(f'could not create an element for "{dev.get_display_name()}"')
    parts = [el.get_factory().get_name()]
    for prop in ("unique-id", "device", "device-name"):
        if el.find_property(prop) is None:
            continue
        val = el.get_property(prop)
        if val:
            parts.append(f'{prop}="{val}"')
    return " ".join(parts)


def has_property(factory_name: str, prop: str) -> bool:
    Gst, _ = gst()
    el = Gst.ElementFactory.make(factory_name, None)
    return el is not None and el.find_property(prop) is not None


def build_send(cfg: dict, tone: bool) -> str:
    if tone or not cfg["device"]:
        head = source_element(cfg, tone)
    else:
        # A named capture source works on any platform, which is how macOS
        # sends: point it at a loopback device such as BlackHole.
        head = device_launch_fragment(find_device(cfg["device"], "Source"))
    # S16BE is mandatory on the wire: RTP L16 is big-endian (RFC 3551).
    return (
        f"{head} "
        f"! audioconvert ! audioresample "
        # Gain is applied in native byte order: the volume element cannot
        # handle S16BE, so the wire-format conversion must come after it.
        f"! volume name=gain volume={cfg['volume']} "
        f"! audioconvert "
        f"! audio/x-raw,format=S16BE,rate={cfg['rate']},"
        f"channels={cfg['channels']},layout=interleaved "
        f"! rtpL16pay pt={cfg['payload']} mtu={cfg['mtu']} "
        f"! udpsink host={cfg['host']} port={cfg['port']} sync=false async=false"
    )


def build_receive(cfg: dict) -> str:
    if cfg["device"]:
        sink = device_launch_fragment(find_device(cfg["device"], "Sink"))
    else:
        sink = "autoaudiosink"
    factory = sink.split()[0]
    tail = ""
    if has_property(factory, "buffer-time"):
        tail += f" buffer-time={cfg['buffer_time']}"
    if has_property(factory, "latency-time"):
        tail += f" latency-time={cfg['latency_time']}"
    return (
        f'udpsrc address={cfg["bind"]} port={cfg["port"]} '
        f'caps="application/x-rtp,media=audio,encoding-name=L16,'
        f'clock-rate={cfg["rate"]},channels={cfg["channels"]},payload={cfg["payload"]}" '
        f"! rtpjitterbuffer name=jb latency={cfg['latency']} "
        f"! rtpL16depay ! audioconvert ! audioresample "
        # Without this explicit format the sink may accept S16BE straight off the
        # wire and play byte-swapped samples, which sounds like loud static.
        f"! audio/x-raw,format={cfg['format']},rate={cfg['rate']},"
        f"channels={cfg['channels']} "
        # Software gain: an OS volume slider does not reliably attenuate a
        # WASAPI loopback tap, and many interfaces have no software volume at
        # all, so the level is controlled here.
        f"! volume name=gain volume={cfg['volume']} "
        f"! {sink} name=sink{tail}"
    )


# ---------------------------------------------------------------- devices

def _devices(kind: str):
    Gst, _ = gst()
    mon = Gst.DeviceMonitor.new()
    mon.add_filter("Audio/" + kind, None)
    mon.start()
    devs = list(mon.get_devices() or [])
    mon.stop()
    return devs


def list_devices(kind: str):
    devs = _devices(kind)
    if not devs:
        print(f"(no Audio/{kind} devices found)")
        return
    for d in devs:
        props = d.get_properties()
        uid = props.get_string("unique-id") if props and props.has_field("unique-id") else ""
        print(f"  {d.get_display_name()}")
        if uid:
            print(f"      unique-id: {uid}")


def find_device(substr: str, kind: str):
    """Return the single Gst.Device whose display name contains substr.

    Works for both Sink and Source on every platform; the caller turns it into
    a configured element with Gst.Device.create_element(), which is portable in
    a way that per-platform device properties are not.
    """
    if not substr:
        return None
    devs = _devices(kind)
    hits = [d for d in devs if substr.lower() in (d.get_display_name() or "").lower()]
    if not hits:
        names = ", ".join(d.get_display_name() for d in devs) or "none"
        die(f'no audio {kind.lower()} matching "{substr}". Available: {names}')
    if len(hits) > 1:
        die(f'"{substr}" is ambiguous: ' + ", ".join(d.get_display_name() for d in hits))
    return hits[0]


# ---------------------------------------------------------------- preflight

def preflight(role: str, cfg: dict, tone: bool) -> list[str]:
    problems: list[str] = []
    Gst, _ = gst(required=(role == "receive"))

    if role == "send" and Gst is None and not find_gst_launch(cfg):
        problems.append(
                "neither GStreamer Python bindings nor gst-launch-1.0 found "
                "(set gst_launch in config, or PCMLINK_GST_LAUNCH)")
    if Gst is not None:
        common = ["audioconvert", "audioresample", "capsfilter"]
        if role == "send":
            need = common + ["rtpL16pay", "udpsink"]
            need += ["audiotestsrc"] if tone else {
                "Windows": ["wasapi2src"], "Linux": ["pulsesrc"]}.get(platform.system(), [])
        else:
            need = common + ["udpsrc", "rtpjitterbuffer", "rtpL16depay"]
            need += ["osxaudiosink"] if platform.system() == "Darwin" else ["autoaudiosink"]
        missing = [e for e in need if Gst.ElementFactory.find(e) is None]
        if missing:
            problems.append(f"missing GStreamer elements: {', '.join(missing)}")

    if not 0.0 <= float(cfg["volume"]) <= 8.0:
        problems.append(f"volume {cfg['volume']} out of range (0.0 to 8.0)")
    if role == "send":
        if not cfg["host"]:
            problems.append(f"no destination host (--host, config, or {APP.upper()}_HOST)")
        else:
            try:
                socket.getaddrinfo(cfg["host"], int(cfg["port"]), type=socket.SOCK_DGRAM)
            except OSError as exc:
                problems.append(f"cannot resolve host {cfg['host']}: {exc}")
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((cfg["bind"], int(cfg["port"])))
        except OSError as exc:
            problems.append(f"cannot bind {cfg['bind']}:{cfg['port']} ({exc}); already running?")
        finally:
            sock.close()
        if cfg["device"] and Gst is not None:
            devs = _devices("Sink")
            hits = [d for d in devs if cfg["device"].lower() in (d.get_display_name() or "").lower()]
            if len(hits) != 1:
                names = ", ".join(d.get_display_name() for d in devs) or "none"
                problems.append(f'device "{cfg["device"]}" matched {len(hits)} sinks. Available: {names}')
    return problems


# ---------------------------------------------------------------- run

def run_subprocess(desc: str, cfg: dict) -> int:
    exe = find_gst_launch(cfg)
    if not exe:
        die("gst-launch-1.0 not found")
    argv = [exe] + shlex.split(desc, posix=(platform.system() != "Windows"))
    print(f"{APP}: using {exe} (no Python bindings; statistics unavailable)", flush=True)
    # Inherit our own streams where they are real files. Under pythonw.exe there
    # are no valid standard handles at all, and a child that inherits them fails
    # immediately and silently, so fall back to discarding output.
    try:
        sys.stdout.fileno()
        out, err = sys.stdout, sys.stderr
    except Exception:
        out = err = subprocess.DEVNULL
    # gst-launch-1.0.exe is a console application: without CREATE_NO_WINDOW,
    # Windows opens a console for it even when the parent is pythonw.exe and the
    # scheduled task is marked hidden.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if platform.system() == "Windows" else 0
    try:
        return subprocess.call(argv, stdout=out, stderr=err, creationflags=flags) if flags \
            else subprocess.call(argv, stdout=out, stderr=err)
    except KeyboardInterrupt:
        return 0


def run(desc: str, cfg: dict, role: str) -> int:
    """Run the pipeline, restarting it in-process if it fails.

    Retrying here rather than relying on an external supervisor keeps the
    process in the session that started it. On macOS that matters: a process
    launched by launchd is not visible to per-application volume tools such as
    SoundSource, while one launched from the GUI session is, so respawning
    must not mean being respawned by a daemon.
    """
    Gst, GLib = gst(required=(role == "receive"))
    if Gst is None:
        return run_subprocess(desc, cfg)
    while True:
        code = _run_once(desc, cfg, role, Gst, GLib)
        if code == 0 or not cfg["retry"]:
            return code
        delay = max(1, int(cfg["retry_delay"]))
        print(f"{APP}: pipeline failed, retrying in {delay}s", flush=True)
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            return 0


def _run_once(desc: str, cfg: dict, role: str, Gst, GLib) -> int:
    try:
        pipeline = Gst.parse_launch(desc)
    except GLib.Error as exc:
        die(f"could not build pipeline: {exc.message}")

    jb = pipeline.get_by_name("jb")
    st = {"t0": None, "p0": 0, "last": 0, "qos": 0, "warn": 0, "failed": False}
    loop = GLib.MainLoop()

    def on_msg(_bus, msg):
        if msg.type == Gst.MessageType.QOS:
            st["qos"] += 1
        elif msg.type == Gst.MessageType.WARNING:
            st["warn"] += 1
            print(f"  !! WARNING {msg.parse_warning()[0].message}", flush=True)
        elif msg.type == Gst.MessageType.ERROR:
            print(f"  !! ERROR {msg.parse_error()[0].message}", flush=True)
            st["failed"] = True
            loop.quit()
        elif msg.type == Gst.MessageType.EOS:
            loop.quit()
        return True

    def u64(s, key):
        for getter in (s.get_uint64, s.get_uint):
            ok, val = getter(key)
            if ok:
                return val
        return 0

    def tick():
        now = time.time()
        if jb is None:
            print(f'{time.strftime("%H:%M:%S")}  sending  '
                  f'qos={st["qos"]} warn={st["warn"]}', flush=True)
            return True
        s = jb.get_property("stats")
        pushed = u64(s, "num-pushed")
        # Start the average at the first packet, not at process start, so an
        # idle wait for the sender does not drag the long-run rate down.
        if st["t0"] is None and pushed > 0:
            st["t0"], st["p0"] = now, pushed
        dt = (now - st["t0"]) if st["t0"] else 0.0
        avg = (pushed - st["p0"]) / dt if dt > 1 else 0.0
        print(f'{time.strftime("%H:%M:%S")}  jb={jb.get_property("percent"):3d}%  '
              f'pkt/s={pushed - st["last"]:4d}  avg={avg:7.2f}  '
              f'lost={u64(s, "num-lost")} late={u64(s, "num-late")} '
              f'dup={u64(s, "num-duplicates")} qos={st["qos"]} warn={st["warn"]}', flush=True)
        st["last"] = pushed
        return True

    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_msg)
    if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        die("pipeline failed to start")
    print(f"{APP} {role}: running (ctrl-c to stop)", flush=True)
    if cfg["stats"]:
        GLib.timeout_add_seconds(1, tick)
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
    finally:
        pipeline.set_state(Gst.State.NULL)
    # Non-zero on failure so launchd/systemd/Task Scheduler restart us.
    return 1 if st["failed"] else 0


# ---------------------------------------------------------------- service
# Supervision is delegated to the platform's own service manager rather than
# reimplemented: launchd on macOS, systemd --user on Linux, Task Scheduler on
# Windows. Each is configured to start at login, run without a window, and
# restart on failure.

SERVICE_LABEL = "io.pcmlink"


def _invocation() -> list[str]:
    """How to re-invoke this program from a service definition.

    shutil.which() must be treated carefully on Windows: PATHEXT includes .PY
    and the current directory is searched, so it happily returns this very
    script and the service ends up invoking the source file as its own
    program. Only accept a genuine console executable.
    """
    console = shutil.which(APP)
    if (console and os.path.abspath(console) != os.path.abspath(__file__)
            and (platform.system() != "Windows" or console.lower().endswith(".exe"))):
        return [console]
    exe = sys.executable
    if platform.system() == "Windows":
        noconsole = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(noconsole):
            exe = noconsole  # no console window on logon
    return [exe, os.path.abspath(__file__)]


def _log_path(role: str) -> str:
    system = platform.system()
    if system == "Darwin":
        base = os.path.expanduser(f"~/Library/Logs/{APP}")
    elif system == "Windows":
        base = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP)
    else:
        base = os.path.join(
            os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), APP)
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{role}.log")


def _plist_path(role: str) -> str:
    return os.path.expanduser(f"~/Library/LaunchAgents/{SERVICE_LABEL}.{role}.plist")


def _unit_path(role: str) -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "systemd", "user", f"{APP}-{role}.service")


def service_install(role: str, extra: list[str], start: bool) -> int:
    from xml.sax.saxutils import escape
    argv = _invocation() + [role] + extra
    system = platform.system()
    log = _log_path(role)

    if system == "Darwin":
        return _install_login_item(role, extra, start)

    if system == "__disabled_launchd__":
        path = _plist_path(role)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        args = "".join(f"    <string>{escape(a)}</string>\n" for a in argv)
        with open(path, "w") as fh:
            fh.write(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict>\n'
                f'  <key>Label</key><string>{SERVICE_LABEL}.{role}</string>\n'
                f'  <key>ProgramArguments</key><array>\n{args}  </array>\n'
                '  <key>RunAtLoad</key><true/>\n'
                '  <key>KeepAlive</key><true/>\n'
                '  <key>ProcessType</key><string>Interactive</string>\n'
                # Pin the agent to the Aqua (GUI login) session. Without this,
                # per-application audio tools such as SoundSource do not see the
                # process and cannot apply their software volume to it.
                '  <key>LimitLoadToSessionType</key><string>Aqua</string>\n'
                f'  <key>StandardOutPath</key><string>{escape(log)}</string>\n'
                f'  <key>StandardErrorPath</key><string>{escape(log)}</string>\n'
                '</dict></plist>\n')
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/{SERVICE_LABEL}.{role}"],
                       capture_output=True)
        # bootout is asynchronous: bootstrapping immediately afterwards can hit a
        # service that is still unloading, so retry briefly before giving up.
        for _ in range(10):
            r = subprocess.run(["launchctl", "bootstrap", domain, path],
                               capture_output=True, text=True)
            if r.returncode == 0:
                break
            time.sleep(0.5)
        else:
            die(f"launchctl bootstrap failed: {r.stderr.strip() or r.returncode}")
        if start:
            subprocess.run(["launchctl", "kickstart", f"{domain}/{SERVICE_LABEL}.{role}"],
                           capture_output=True)
        print(f"installed {path}\nlogs: {log}")
        return 0

    if system == "Linux":
        path = _unit_path(role)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        cmd = " ".join(shlex.quote(a) for a in argv)
        with open(path, "w") as fh:
            fh.write(
                "[Unit]\n"
                f"Description={APP} {role}\n"
                "After=default.target\n\n"
                "[Service]\n"
                "Type=simple\n"
                f"ExecStart={cmd}\n"
                "Restart=always\n"
                "RestartSec=5\n\n"
                "[Install]\n"
                "WantedBy=default.target\n")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        args = ["systemctl", "--user", "enable"] + (["--now"] if start else []) + [f"{APP}-{role}.service"]
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode != 0:
            die(f"systemctl failed: {r.stderr.strip() or r.returncode}")
        print(f"installed {path}\nlogs: journalctl --user -u {APP}-{role} -f")
        return 0

    if system == "Windows":
        name = f"{APP}-{role}"
        # Task Scheduler captures no output at all, so the program logs itself.
        # --log is an option of pcmlink, not of the interpreter, so it must come
        # after the whole invocation prefix (python.exe plus the script path).
        argv = _invocation() + ["--log", log, role] + extra
        cmd = " ".join(f'"{a}"' if " " in a else a for a in argv)
        r = subprocess.run(
            ["schtasks", "/create", "/tn", name, "/tr", cmd, "/sc", "onlogon",
             "/rl", "limited", "/f"], capture_output=True, text=True)
        if r.returncode != 0:
            die(f"schtasks failed: {(r.stderr or r.stdout).strip()}")
        # Restart-on-failure is not expressible via schtasks flags; set it on the
        # registered task definition through the Schedule.Service COM object.
        ps = (
            f"$s=New-Object -ComObject Schedule.Service; $s.Connect(); "
            f"$t=$s.GetFolder('\\').GetTask('{name}'); $d=$t.Definition; "
            "$d.Settings.RestartCount=3; $d.Settings.RestartInterval='PT1M'; "
            "$d.Settings.Hidden=$true; $d.Settings.ExecutionTimeLimit='PT0S'; "
            "$d.Settings.DisallowStartIfOnBatteries=$false; "
            "$d.Settings.StopIfGoingOnBatteries=$false; "
            f"$s.GetFolder('\\').RegisterTaskDefinition('{name}',$d,4,$null,$null,3) | Out-Null"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
        if start:
            subprocess.run(["schtasks", "/run", "/tn", name], capture_output=True)
        print(f"registered scheduled task {name} (at logon, hidden, restarts on failure)")
        return 0
    die(f"service management is not implemented for {system}")


def _app_bundle_path() -> str:
    return os.path.join(os.path.expanduser("~/Applications"), f"{APP}.app")


def _build_app_bundle(role: str, extra: list[str]) -> str:
    """Create a windowless .app that owns both its interpreter and its script.

    LaunchServices starts this in the GUI login session, which is what keeps the
    process visible to per-application audio tools such as SoundSource; a
    launchd agent is not. The bundle executable is a launcher that execs the
    interpreter copied *inside* the bundle, so after exec the running image is
    still a bundle path rather than a system-wide interpreter.
    """
    app = _app_bundle_path()
    macos_dir = os.path.join(app, "Contents", "MacOS")
    res_dir = os.path.join(app, "Contents", "Resources")
    os.makedirs(macos_dir, exist_ok=True)
    os.makedirs(res_dir, exist_ok=True)

    interpreter = os.path.join(macos_dir, f"{APP}-python")
    shutil.copy2(os.path.realpath(sys.executable), interpreter)
    os.chmod(interpreter, 0o755)
    script = os.path.join(res_dir, f"{APP}.py")
    shutil.copy2(os.path.abspath(__file__), script)

    quoted = " ".join(shlex.quote(a) for a in [role] + extra)
    launcher = os.path.join(macos_dir, APP)
    with open(launcher, "w") as fh:
        fh.write(
            "#!/bin/sh\n"
            'dir=$(cd "$(dirname "$0")" && pwd)\n'
            f'exec "$dir/{APP}-python" "$dir/../Resources/{APP}.py" {quoted}\n')
    os.chmod(launcher, 0o755)

    with open(os.path.join(app, "Contents", "Info.plist"), "w") as fh:
        fh.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            f'  <key>CFBundleName</key><string>{APP}</string>\n'
            f'  <key>CFBundleIdentifier</key><string>{SERVICE_LABEL}.{role}</string>\n'
            f'  <key>CFBundleExecutable</key><string>{APP}</string>\n'
            '  <key>CFBundlePackageType</key><string>APPL</string>\n'
            '  <key>CFBundleVersion</key><string>0.1.0</string>\n'
            '  <key>LSUIElement</key><true/>\n'
            '</dict></plist>\n')
    return app


def _install_login_item(role: str, extra: list[str], start: bool) -> int:
    app = _build_app_bundle(role, extra)
    subprocess.run(["/System/Library/Frameworks/CoreServices.framework/Frameworks/"
                    "LaunchServices.framework/Support/lsregister", "-f", app],
                   capture_output=True)
    script = (f'tell application "System Events" to delete '
              f'(every login item whose name is "{APP}")')
    subprocess.run(["osascript", "-e", script], capture_output=True)
    script = (f'tell application "System Events" to make login item at end '
              f'with properties {{path:"{app}", hidden:true}}')
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        die(f"could not add login item: {r.stderr.strip()}")
    if start:
        subprocess.run(["open", "-a", app], capture_output=True)
    print(f"installed {app} as a hidden login item\n"
          f"logs: {_log_path(role)}")
    return 0


def service_uninstall(role: str) -> int:
    system = platform.system()
    if system == "Darwin":
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/{SERVICE_LABEL}.{role}"],
                       capture_output=True)
        path = _plist_path(role)
        if os.path.exists(path):
            os.remove(path)
        print(f"removed {path}")
    elif system == "Linux":
        subprocess.run(["systemctl", "--user", "disable", "--now", f"{APP}-{role}.service"],
                       capture_output=True)
        path = _unit_path(role)
        if os.path.exists(path):
            os.remove(path)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        print(f"removed {path}")
    elif system == "Windows":
        subprocess.run(["schtasks", "/delete", "/tn", f"{APP}-{role}", "/f"], capture_output=True)
        print(f"removed scheduled task {APP}-{role}")
    else:
        die(f"service management is not implemented for {system}")
    return 0


def service_status(role: str) -> int:
    system = platform.system()
    if system == "Darwin":
        r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{SERVICE_LABEL}.{role}"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("not installed")
            return 1
        for line in r.stdout.splitlines():
            if any(k in line for k in ("state =", "pid =", "last exit", "runs =")):
                print(line.strip())
    elif system == "Linux":
        subprocess.run(["systemctl", "--user", "status", "--no-pager", f"{APP}-{role}.service"])
    elif system == "Windows":
        subprocess.run(["schtasks", "/query", "/tn", f"{APP}-{role}", "/v", "/fo", "list"])
    else:
        die(f"service management is not implemented for {system}")
    print(f"logs: {_log_path(role)}")
    return 0


# ---------------------------------------------------------------- cli

def add_shared(sp):
    sp.add_argument("--port", type=int, help=SETTINGS["port"][1])
    sp.add_argument("--rate", type=int, help=SETTINGS["rate"][1])
    sp.add_argument("--channels", type=int, help=SETTINGS["channels"][1])
    sp.add_argument("--payload", type=int, help=SETTINGS["payload"][1])
    sp.add_argument("--device", help=SETTINGS["device"][1])
    sp.add_argument("--volume", type=float, help=SETTINGS["volume"][1])
    sp.add_argument("--gst-launch", dest="gst_launch", help=SETTINGS["gst_launch"][1])
    sp.add_argument("--no-stats", dest="stats", action="store_false", default=None,
                    help="suppress the per-second statistics line")
    sp.add_argument("--dry-run", action="store_true",
                    help="run preflight, print the pipeline, and exit")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog=APP, description=__doc__.splitlines()[0])
    p.add_argument("--config", help="explicit config file, bypassing the search path")
    p.add_argument("--log", help="append output to this file instead of stdout")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("send", help="capture system audio and stream it")
    add_shared(sp)
    sp.add_argument("--host", help=SETTINGS["host"][1])
    sp.add_argument("--mtu", type=int, help=SETTINGS["mtu"][1])
    sp.add_argument("--test-tone", action="store_true",
                    help="send a sine wave instead of captured audio")
    sp.add_argument("--tone-freq", dest="tone_freq", type=int, help=SETTINGS["tone_freq"][1])
    sp.add_argument("--tone-volume", dest="tone_volume", type=float, help=SETTINGS["tone_volume"][1])

    rp = sub.add_parser("receive", help="play an incoming stream")
    add_shared(rp)
    rp.add_argument("--bind", help=SETTINGS["bind"][1])
    rp.add_argument("--latency", type=int, help=SETTINGS["latency"][1])
    rp.add_argument("--buffer-time", dest="buffer_time", type=int, help=SETTINGS["buffer_time"][1])
    rp.add_argument("--latency-time", dest="latency_time", type=int, help=SETTINGS["latency_time"][1])
    rp.add_argument("--format", help=SETTINGS["format"][1])

    dp = sub.add_parser("devices", help="list audio devices")
    dp.add_argument("--kind", choices=["Sink", "Source"], default="Sink")

    cp = sub.add_parser("config", help="show the config search path and effective values")
    cp.add_argument("--role", choices=["send", "receive"], default="receive")

    vp = sub.add_parser("service",
                        help="install, remove or inspect the background service")
    vp.add_argument("action", choices=["install", "uninstall", "status"])
    vp.add_argument("role", choices=["send", "receive"])
    vp.add_argument("--no-start", dest="start", action="store_false", default=True,
                    help="install without starting it now")
    vp.add_argument("args", nargs=argparse.REMAINDER,
                    help="arguments for the role, after --")

    a = p.parse_args(argv)

    if getattr(a, "log", None):
        os.makedirs(os.path.dirname(os.path.abspath(a.log)) or ".", exist_ok=True)
        # Held open for the lifetime of the process on purpose.
        fh = open(a.log, "a", buffering=1)  # noqa: SIM115
        sys.stdout = fh
        sys.stderr = fh

    if a.cmd == "devices":
        list_devices(a.kind)
        return 0

    if a.cmd == "service":
        extra = [x for x in a.args if x != "--"]
        if a.action == "install":
            return service_install(a.role, extra, a.start)
        if a.action == "uninstall":
            return service_uninstall(a.role)
        return service_status(a.role)

    cfg_data, used = load_config(getattr(a, "config", None))

    if a.cmd == "config":
        print("search path (least important first):")
        for path in config_paths():
            print(f"  {'[loaded] ' if path in used else '          '}{path}")
        print("\neffective values:")
        eff = resolve(argparse.Namespace(), cfg_data, a.role)
        for key in SETTINGS:
            marker = "" if eff[key] == DEFAULTS[key] else "  <- overridden"
            print(f"  {key:14} {eff[key]!r}{marker}")
        return 0

    role = a.cmd
    cfg = resolve(a, cfg_data, role)
    tone = getattr(a, "test_tone", False)

    problems = preflight(role, cfg, tone)
    if problems:
        for problem in problems:
            print(f"{APP}: preflight: {problem}", file=sys.stderr)
        return 2

    desc = build_send(cfg, tone) if role == "send" else build_receive(cfg)

    if a.dry_run:
        print("config: " + (", ".join(used) if used else "defaults only"))
        print("preflight: ok")
        print("pipeline:\n  " + desc)
        return 0
    return run(desc, cfg, role)


if __name__ == "__main__":
    sys.exit(main())
