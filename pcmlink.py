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
    "stats":        (True,     "print one statistics line per second"),
    "tone_freq":    (440,      "test tone frequency in Hz"),
    "tone_volume":  (0.05,     "test tone volume, 0.0 to 1.0"),
    "gst_launch":   ("",       "path to gst-launch-1.0 (blank = search PATH)"),
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

    if role == "send" and Gst is None:
        if not find_gst_launch(cfg):
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
    try:
        return subprocess.call(argv)
    except KeyboardInterrupt:
        return 0


def run(desc: str, cfg: dict, role: str) -> int:
    Gst, GLib = gst(required=(role == "receive"))
    if Gst is None:
        return run_subprocess(desc, cfg)
    try:
        pipeline = Gst.parse_launch(desc)
    except GLib.Error as exc:
        die(f"could not build pipeline: {exc.message}")

    jb = pipeline.get_by_name("jb")
    st = {"t0": None, "p0": 0, "last": 0, "qos": 0, "warn": 0}
    loop = GLib.MainLoop()

    def on_msg(_bus, msg):
        if msg.type == Gst.MessageType.QOS:
            st["qos"] += 1
        elif msg.type == Gst.MessageType.WARNING:
            st["warn"] += 1
            print(f"  !! WARNING {msg.parse_warning()[0].message}", flush=True)
        elif msg.type == Gst.MessageType.ERROR:
            print(f"  !! ERROR {msg.parse_error()[0].message}", flush=True)
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
    return 0


# ---------------------------------------------------------------- cli

def add_shared(sp):
    sp.add_argument("--port", type=int, help=SETTINGS["port"][1])
    sp.add_argument("--rate", type=int, help=SETTINGS["rate"][1])
    sp.add_argument("--channels", type=int, help=SETTINGS["channels"][1])
    sp.add_argument("--payload", type=int, help=SETTINGS["payload"][1])
    sp.add_argument("--device", help=SETTINGS["device"][1])
    sp.add_argument("--gst-launch", dest="gst_launch", help=SETTINGS["gst_launch"][1])
    sp.add_argument("--no-stats", dest="stats", action="store_false", default=None,
                    help="suppress the per-second statistics line")
    sp.add_argument("--dry-run", action="store_true",
                    help="run preflight, print the pipeline, and exit")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog=APP, description=__doc__.splitlines()[0])
    p.add_argument("--config", help="explicit config file, bypassing the search path")
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

    a = p.parse_args(argv)

    if a.cmd == "devices":
        list_devices(a.kind)
        return 0

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
