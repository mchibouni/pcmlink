# pcmlink

Point-to-point uncompressed audio over a LAN. Captures audio on one machine and
plays it through a chosen audio device on another, as RTP/L16 over UDP.

No compression, no discovery, no GUI, no account, no cloud. One sender, one
receiver, one UDP port.

## Platform support

Any machine can receive. Sending needs something to capture: Windows and Linux
can capture what the system is playing without extra software, and every
platform can capture a named input device.

| | Windows | Linux | macOS |
|---|---|---|---|
| Receive, with device selection | yes | yes | yes |
| Send: capture system output | yes (WASAPI loopback) | yes (sink monitor) | needs a loopback device¹ |
| Send: capture a named input | yes | yes | yes |
| Send: capture a JACK graph | no | yes (`--jack`) | no |
| Receive: play into a JACK graph | no | yes (`--jack`) | no |
| Live statistics | needs Python bindings | needs Python bindings | needs Python bindings |

¹ macOS has no built-in way to capture its own output. Install a loopback
device such as BlackHole and pass it with `--device`; it then behaves like any
other capture source.

Device selection works everywhere because devices are resolved through
GStreamer's device monitor, which configures the platform's own element —
`osxaudiosink`, `pulsesink`, `wasapi2sink` — rather than assuming one of them.

## Requirements

GStreamer 1.20 or newer on both machines.

The receiver additionally wants the GStreamer Python bindings, which is how it
reports live statistics. The sender works without them by invoking
`gst-launch-1.0`, which is the normal situation on Windows.

```sh
# macOS
brew install gstreamer pygobject3

# Debian/Ubuntu
sudo apt install python3-gi gstreamer1.0-plugins-base \
                 gstreamer1.0-plugins-good gstreamer1.0-plugins-bad

# Windows: the official MSI from gstreamer.freedesktop.org, complete install
```

## Use

```sh
pcmlink devices                      # what can I play to?
pcmlink devices --kind Source        # what can I capture from?

pcmlink receive --device Scarlett    # on the machine with the audio interface
pcmlink send --host 192.168.1.50     # on the machine making the sound

pcmlink send --host 192.168.1.50 --test-tone --tone-volume 0.02
pcmlink receive --device Scarlett --dry-run
```

Devices are named by a case-insensitive substring, never a raw device ID. A
name that matches nothing, or matches more than one device, is a preflight
failure that lists what is actually available.

`--test-tone` replaces captured audio with a sine wave. It is the fastest way
to tell a transport problem from a capture problem: if the tone is clean and
real audio is not, the fault is on the capture side.

## Running it in the background

`pcmlink service` hands supervision to the platform's own service manager
rather than reimplementing it. In every case the service starts at login, runs
without a window, and is restarted if it exits.

```sh
pcmlink service install receive -- --device Scarlett --volume 0.6
pcmlink service status receive
pcmlink service uninstall receive
```

Arguments after `--` are passed to the role, so anything that works on the
command line works as a service.

| Platform | Mechanism | Restart behaviour |
|---|---|---|
| macOS | hidden login item (`~/Applications/pcmlink.app`) | in-process, `retry_delay` seconds |
| Linux | systemd `--user` unit; `send` is bound to `graphical-session.target` | `Restart=always`, `RestartSec=5` |
| Windows | Task Scheduler, at logon, hidden | 3 retries at 1-minute intervals |

Windows restarts are noticeably slower than the other two: Task Scheduler's
minimum retry interval is one minute, so a crash there costs a minute of
silence rather than seconds.

**macOS deliberately does not use a launchd agent.** A process started by
launchd is not visible to per-application audio tools — SoundSource could not
see it or apply its software volume, which matters because many interfaces
expose no volume to macOS at all and such a tool is the only control available.
A process launched by LaunchServices from the GUI session is visible, so the
macOS service is a windowless login item whose bundle owns both its interpreter
and its script. For the same reason the receiver restarts its own pipeline
in-process (`retry`) rather than exiting to be respawned by a supervisor.

Logs go to `~/Library/Logs/pcmlink/` on macOS, the journal on Linux
(`journalctl --user -u pcmlink-receive -f`), and `%LOCALAPPDATA%\pcmlink\` on
Windows. Task Scheduler captures no output of its own, so the program writes
its own log there via `--log`.

### Controlling both ends at once

`contrib/pcmlink-ctl` is a small shell wrapper for the common two-machine case:
a receiver here, a sender running as a scheduled task on a Windows host
reachable over ssh.

```sh
pcmlink-ctl status      # both ends, and whether packets are actually moving
pcmlink-ctl restart     # restart both, then report
pcmlink-ctl restart-tx  # sender only
pcmlink-ctl start-rx    # receiver only, and only if it is not already running
pcmlink-ctl logs        # follow the receiver log
```

`status` samples the log twice and requires a non-zero packet rate, so a
receiver that is running while the sender is silent reports as **no packets**
rather than a false green. It also shows the output device and the sample rate
it is running at, which is the rate set by whatever the interface is clocked
from when it is not the clock master.

`start-rx` does nothing if a receiver is already running, so it is safe to run
at login from something that runs inside your desktop session, such as
Hammerspoon or a login item. Don't use launchd: a receiver started by launchd
is invisible to SoundSource.

## Which endpoint to capture

On Windows this choice decides whether the *sending* machine's volume control
works, and it is not obvious.

A loopback tap is taken after the software volume stage only on endpoints where
Windows inserts one. On endpoints that report hardware volume support — which
includes many virtual sound cards — the tap is full-scale and the volume slider
has no effect on what is sent.

So capture a **real output endpoint** if you want the sending machine's volume
keys to work. Capturing a virtual device gives a stream fixed at full scale,
adjustable only at the receiving end.

With a real endpoint the two controls compose naturally: the sender's volume
sets the source level, and the receiver's own control sets monitoring level,
exactly as an application's volume slider and a speaker knob do.

Two consequences worth knowing:

- Anything physically connected to that endpoint will also play the audio. Mute
  it at the device; that does not affect what is captured.
- An HDMI endpoint can become inactive when its display sleeps or switches
  input, which takes the capture with it. A virtual endpoint is more stable but
  costs you the sender-side volume control.

On Linux the sender captures the default sink's monitor, and PipeWire takes a
monitor **before** the sink's volume by default, so the volume keys have no
effect on what is sent. Set `monitor.channel-volumes` on the sink to tap it
after the volume stage instead. Live, for sink id `56`:

```sh
pw-cli set-param 56 Props '{ params = [ "monitor.channel-volumes" true ] }'
```

and persistently with a WirePlumber rule, which applies from the next time
WirePlumber starts:

```
# ~/.config/wireplumber/wireplumber.conf.d/51-monitor-volume.conf
monitor.alsa.rules = [
  {
    matches = [ { node.name = "alsa_output.pci-0000_2f_00.4.iec958-stereo" } ]
    actions = { update-props = { monitor.channel-volumes = true } }
  }
]
```

Verified with a −37 dBFS tone at sink volume 1.0 / 0.54 / 0.25: the tap read
−37 dBFS at every setting by default, and −37 / −53 / −73 dBFS with the property
set.

## JACK systems (norns)

A machine that runs JACK alone, with no PipeWire or PulseAudio, has no sink
monitor to capture. `--jack` takes a regular expression naming JACK output
ports and taps them directly. A monome norns is the case this was written for:
its whole mix, after the reverb, compressor and tape, leaves on
`crone:output_1` and `crone:output_2`.

```sh
pcmlink send --host 192.168.1.50 --port 5006 --jack 'crone:output_[12]'
```

Run it as the user that owns the JACK server. Without a pattern JACK would
connect the *physical capture* ports, which are the inputs.

Notes from a norns shield (Raspberry Pi 3B+, Debian 11):

- Debian 11 has GStreamer 1.18, which is enough for a JACK sender:
  `sudo apt install --no-install-recommends gstreamer1.0-tools
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good` (`jackaudiosrc` is in
  plugins-good). The sender then runs through `gst-launch-1.0`.
- Python 3.9 is fine without a config file; use flags or `PCMLINK_*` variables,
  or install `tomli` to read one.
- Run it as a **system** unit with `User=` set to the JACK user, ordered after
  the unit that creates the ports, and `PartOf=` that unit: JACK drops the
  connections when the port owner restarts, and nothing reconnects them.
  On a norns that is `After=norns-crone.service`, `PartOf=norns-crone.service`,
  `WantedBy=norns.target`.
- If new JACK clients fail with "Cannot connect to server socket" while JACK is
  plainly running, logind has removed JACK's files in `/dev/shm` at the end of
  an ssh session. Set `RemoveIPC=no` in a `logind.conf.d` drop-in (norns does so
  upstream from late 2026) and restart JACK.
- Give it its own port and its own receiver. Two senders on one port arrive as
  one stream with clashing sequence numbers, which the receiver scores as heavy
  packet loss. `contrib/pcmlink-ctl` runs the norns link on 5006 next to the
  main one.

The other way, `receive --jack` plays into JACK *input* ports instead of an
audio device. On a norns, `crone:input_1` and `crone:input_2` are its input,
so scripts process the stream as if it came in through the jack:

```sh
pcmlink receive --port 5008 --jack 'crone:input_[12]'
```

A JACK receiver needs no Python bindings (it looks up no device), so it runs
through `gst-launch-1.0` on a stock norns.

### Feeding a norns from a Mac

`contrib/systap` captures the Mac's system audio with a Core Audio process tap
(macOS 14.2+), so no loopback device is needed, and leaves out the processes
named with `--exclude-pid`. `pcmlink-ctl start-norns-in` uses it to send the
Mac's audio to a norns receiver on port 5008, leaving out the norns' own
receiver and SoundSource's `arkaudiod` (which replays the audio it processes).
Otherwise the norns' sound would come back into the capture and loop. macOS
asks once for System Audio Recording permission for the app that runs it.

```sh
swiftc -O contrib/systap/systap.swift -o contrib/systap/systap
pcmlink-ctl start-norns-in      # and stop-norns-in
```

## Volume

`--volume` applies a software gain: `1.0` is unity, `0.5` is roughly −6 dB,
`2.0` is +6 dB. It is available on both roles, applied after capture on the
sender and before the output device on the receiver.

This exists because the operating system's volume slider often does **not**
affect what pcmlink sends. A WASAPI loopback tap is only attenuated when
Windows inserts a volume APO into the software audio engine; on an endpoint
with hardware volume support — and on most virtual sound cards — the tap is
full-scale no matter where the slider sits. Many audio interfaces also have no
software volume at all, only a physical knob. `--volume` is the reliable
control.

## Statistics

The receiver prints one line per second:

```
14:01:09  jb= 99%  pkt/s= 200  avg= 201.08  lost=0 late=0 dup=0 qos=0 warn=0
```

- `jb` — jitterbuffer fill; steady near 100% is healthy, sagging means starvation
- `pkt/s` / `avg` — instantaneous and cumulative packet rate
- `lost` / `late` / `dup` — packets missing, too late to use, or repeated
- `qos` — buffers the output device dropped
- `warn` — pipeline warnings, also printed in full

`avg` is the useful long-run number. Its absolute value depends on how many
frames the capture source puts in each buffer, so it differs between sources
and is not a fixed target: what matters is that it settles and then stays put.
A slow monotonic drift in `avg`, or in `jb` over tens of minutes, is clock
drift between the two machines. The averaging window starts at the first packet
received, not at process start.

Note that GStreamer's jitterbuffer performs its own clock-skew correction, so a
clean log proves the link is *stable*, not that there is no drift.

## Configuration

`pcmlink config` prints the search path and the effective values.

Files are layered, following the XDG Base Directory Specification on Unix —
user configuration overrides system configuration — with Windows using
`%PROGRAMDATA%` then `%APPDATA%`. Later sources override earlier ones:

```
defaults < system config < user config < ./pcmlink.toml < PCMLINK_* env < CLI flags
```

Any setting can be given as an environment variable: `PCMLINK_PORT=6000`,
`PCMLINK_DEVICE=Scarlett`. See `pcmlink.example.toml` for the full set.

## Implementation notes

Two details cost real debugging time and are worth knowing before modifying the
pipelines:

- **RTP L16 is big-endian** (RFC 3551). If the output element advertises support
  for `S16BE`, GStreamer will hand it byte-swapped samples and the result is
  loud static rather than an error. The receiver forces an explicit output
  format so the conversion actually happens.
- **Do not set `low-latency=true` on `wasapi2src`.** It starves capture and
  produces continuous clicking that looks exactly like packet loss in the
  statistics, while `lost` stays at zero.
- **Big-endian audio is poorly supported by most elements**, which bites more
  than once. `volume` cannot process `S16BE`, so on the sender the gain is
  applied in native byte order and converted afterwards.
- **A hidden scheduled task does not hide its children.** `gst-launch-1.0.exe`
  is a console application, so Windows opens a console for it even when the
  parent is `pythonw.exe` and the task is marked hidden. The child must be
  spawned with `CREATE_NO_WINDOW`.
- **`shutil.which()` is unsafe for locating an installed console script on
  Windows**: `PATHEXT` includes `.PY` and the current directory is searched, so
  it will cheerfully return the source file and a generated service will try to
  execute the script as its own program.

## Limitations

- One sender per receiver. There is no session negotiation: both ends must
  agree on sample rate, channel count and payload type.
- Unicast only, and unencrypted. Use it on a network you trust.
- Latency is a buffering choice, not a guarantee. The defaults favour
  robustness over latency and suit listening rather than monitoring.

## Licence

MIT
