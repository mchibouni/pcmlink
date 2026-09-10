# pcmlink

Uncompressed PCM audio over a LAN. Captures system audio on one machine and
plays it through a chosen audio interface on another, as RTP/L16 over UDP.

No compression, no discovery, no GUI, no account. One sender, one receiver,
one UDP port.

Built for a specific gap: getting audio out of a Windows VM (or a Linux
desktop) and into the audio interface attached to a Mac, without a virtual
sound-card driver on the sending side or a GUI application on either end.

## Requirements

GStreamer 1.20 or newer on both machines.

| Role | Needs |
|---|---|
| receive | GStreamer + its Python bindings (for live statistics) |
| send | GStreamer; the Python bindings are optional — `gst-launch-1.0` is used if absent |

```sh
# macOS
brew install gstreamer pygobject3

# Debian/Ubuntu
sudo apt install python3-gi gstreamer1.0-plugins-base \
                 gstreamer1.0-plugins-good gstreamer1.0-plugins-bad

# Windows: the official MSI from gstreamer.freedesktop.org (choose the
# complete install). Python bindings are not required for sending.
```

## Use

```sh
# What can I play to?
pcmlink devices

# Receiver, on the machine with the audio interface
pcmlink receive --device Scarlett

# Sender, on the machine making the sound
pcmlink send --host 192.168.1.50

# Prove the path works before blaming the capture side
pcmlink send --host 192.168.1.50 --test-tone --tone-volume 0.02

# Show the pipeline and validate everything, without starting audio
pcmlink receive --device Scarlett --dry-run
```

Devices are selected by a case-insensitive substring of their name, not by a
raw device ID. An ambiguous or unmatched name is a preflight failure that
lists what is actually available.

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
and is not a fixed target: what matters is that it *settles* and then stays
put. A slow, monotonic drift in `avg`, or in `jb` over tens of minutes, is
clock drift between the two machines. The averaging window starts at the first
packet received, not at process start.

Note that GStreamer's jitterbuffer performs its own clock-skew correction, so
a clean log proves the link is *stable*, not that there is no drift.

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

## Notes from the implementation

Two things cost real debugging time and are worth knowing if you modify the
pipelines:

- **RTP L16 is big-endian** (RFC 3551). If the output element advertises
  support for `S16BE`, GStreamer will happily hand it byte-swapped samples
  and the result is loud static rather than an error. The receiver forces an
  explicit output format to make the conversion happen.
- **Do not set `low-latency=true` on `wasapi2src`.** It starves capture and
  produces continuous clicking. This is a capture-side fault that looks
  exactly like packet loss; `--test-tone` distinguishes the two in seconds.

## Limitations

- macOS can receive but not send: it has no system-audio capture without a
  third-party virtual device.
- One sender per receiver. There is no session negotiation; both ends must
  agree on rate, channels and payload type.
- Unicast only, and no encryption. Use it on a network you trust.

## Licence

MIT
