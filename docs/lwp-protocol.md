# LWP — Lynx Window Protocol (v1)

LWP is lynxde's own display protocol and compositor architecture. It is a
standalone protocol: the wire format, object model, buffer lifetime rules and
compositor are all native to lynxde. Wayland is not involved anywhere in the
core — legacy apps are served by an optional sidecar bridge process that
*translates into* LWP, never the other way around.

Design goals, in order:

1. **Memory safety by construction** — every allocation is bounded, every
   buffer has an explicit generation-stamped lifetime, no message can cause
   out-of-bounds work, and misuse degrades to a catchable exception or a
   disconnect instead of corruption.
2. **Speed** — fixed-size binary framing, zero-copy pixel handoff through
   shared memfd mappings, batched damage rectangles, single-dispatch epoll,
   and frame pacing driven by presentation timestamps.
3. **Backwards compatibility** — existing X11 and Wayland clients keep
   running via `lynx-bridge` (+ `Xwayland` for X11), and existing lynxde
   sessions on Hyprland keep working unchanged.

## Topology

```
+------------------------------------------------------+
|                  lynx-compositor (LWP server)         |
|  lwp_core.py    state machine, layout, input, damage  |
|  backends       kms (direct scanout) | qt-nested      |
|  control.sock   JSON control plane (hyprctl-like)     |
+----+-----------------------------+-------------------+
     |                             |
     |  native LWP clients         |  optional sidecar
     |  (lynx-taskbar, wallpaper,  |  lynx-bridge:
     |   launcher, settings, ...)  |  wayland/X11 apps -> LWP
     v                             v
 $XDG_RUNTIME_DIR/lynx/lwp.sock    $XDG_RUNTIME_DIR/wayland-lwp-0
```

Native clients speak LWP directly on one unix socket. The bridge is an
ordinary LWP client that additionally speaks Wayland to old apps; if it dies,
native traffic is unaffected.

## Wire format

All integers little-endian. A *frame* is a 16-byte header followed by a
payload of at most `MAX_PAYLOAD` bytes:

```
offset  size  field
0       2     magic   = 0x574C ('L','W')
2       1     version = 1
3       1     flags   bit0: frame carries fds (SCM_RIGHTS); bits1-7 reserved 0
4       4     length  payload byte count (u32)
8       2     kind    message kind (u16)
10      2     items   batch item count for batched kinds, else 0
12      4     seq     sender sequence number (u32, monotonic per connection)
```

Rules enforced by both sides before any decode:

- magic/version mismatch → `ProtocolError`, connection closed.
- `length > MAX_PAYLOAD` (16 MiB) → `ProtocolError`. No incremental buffering
  beyond one maximum frame; a peer cannot make the receiver grow memory.
- unknown kind, reserved flag bits set, or truncated payload → `ProtocolError`.
- Strings appear only in low-frequency kinds (`HELLO`, `SET_TITLE`,
  `OUTPUT_INFO`) as `u32 byte_len + utf-8 bytes`; `byte_len` is validated
  against remaining payload before slicing.
- At most 4 fds per frame; fds arrive only with flag bit0 set; fds are opened
  `O_CLOEXEC` and counted against a per-connection in-flight cap.

### Kinds

Client → server:

| kind | name            | payload (after required u32 surface_id where noted) |
|-----:|-----------------|------------------------------------------------------|
| 1    | HELLO           | u32 proto_version, u32 caps, string name             |
| 2    | CREATE_SURFACE  | u32 surface_id (client-chosen), u32 role             |
| 3    | DESTROY_SURFACE | u32 surface_id                                       |
| 4    | SET_GEOM        | id, i32 x, y, w, h, u32 anchor bits, i32 exclusive   |
| 5    | ATTACH          | id, u32 width, height, stride, format, u64 buf_gen   |
| 6    | DAMAGE          | id + items × (i32 x, y, w, h)   [batched]            |
| 7    | COMMIT          | id, u32 commit_flags                                 |
| 8    | SET_TITLE       | id, string title                                     |
| 9    | REQUEST_CLOSE   | id                                                   |
| 10   | PING            | u32 nonce                                            |

Server → client (kinds ≥ 128):

| kind | name          | payload                                             |
|-----:|---------------|------------------------------------------------------|
| 129  | WELCOME       | u32 proto_version, u32 caps, u32 client_id, string name |
| 130  | SURFACE_ACK   | u32 surface_id, u32 result (OK/DUP/BAD_ROLE/MAX)     |
| 131  | CONFIGURE     | id, i32 x, y, w, h, u32 state (bit0 focused)         |
| 132  | FRAME_DONE    | id, u64 presentation_ts_ns                           |
| 133  | INPUT_ENTER   | id                                                   |
| 134  | INPUT_LEAVE   | id                                                   |
| 135  | POINTER_MOTION| f32 x, f32 (surface-local)                           |
| 136  | POINTER_BUTTON| u32 button (evdev code), u32 pressed                 |
| 137  | POINTER_AXIS  | f32 dx, dy                                           |
| 138  | KEY           | u32 keycode (evdev), u32 pressed, u32 mods           |
| 139  | CLOSE_REQ     | id                                                   |
| 140  | OUTPUT_INFO   | u32 output_id, i32 w, h, u32 refresh_millihz, string |
| 141  | SHUTDOWN      | u32 reason                                           |
| 142  | PONG          | u32 nonce                                            |

Roles: `WINDOW=0, BACKGROUND=1, PANEL=2, TITLEBAR=3, OVERLAY=4`.
Formats: `XRGB8888 = 0` (fourcc XR24), `ARGB8888 = 1` (premultiplied AR24).

Handshake: client sends `HELLO` as its first frame; anything else is a
protocol violation. Server replies `WELCOME` or closes. Surfaces may be
created after `WELCOME`.

## Buffer model (the memory-safety core)

Pixels move through shared memory files created by the client with
`memfd_create(2)` and sealed before first attach:

- `F_SEAL_SHRINK | F_SEAL_GROW` — the size can never change under the
  compositor's feet (the classic wl_shm resize race is impossible).
- `F_SEAL_WRITE` after the initial fill — the mapping the compositor reads
  cannot be re-written through the file; in-flight writes come only from the
  sender's private writable mapping.

The compositor validates `stride * height <= file_size` before ever touching
bytes, mmaps once per buffer, and caches mappings keyed by `(fd identity,
buf_gen)`. Every attach carries a monotonically increasing `u64 buf_gen`;
a commit referencing anything other than the newest generation for a surface
is dropped without being read. Destroying a surface unmaps and closes every
buffer it owned exactly once (idempotent release). There is no way to make
the compositor read freed, shrunk or foreign memory through this path.

Per-connection hard limits (violations disconnect the client, not the
compositor): 256 live surfaces, 16 MiB payload, 64 damage rects per DAMAGE
frame, 8 in-flight fds, 1024 pending outbound frames.

## Efficiency model

- One syscall family per frame: clients `memcpy` pixels into their sealed
  memfd and send `ATTACH` once per buffer *size*, then `COMMIT` per frame.
  No fd passing, no copies through sockets, no protocol chatter per frame.
- Damage is expressed as rectangle batches (one frame, N rects, coalesced by
  the compositor's accumulator). Untouched surfaces cost nothing to recomposite.
- Presentation is paced by `FRAME_DONE` carrying the compositor's presentation
  timestamp; clients render at most one frame ahead.
- Fixed 16-byte headers and cached `struct.Struct` codecs keep the hot path
  allocation-free in CPython terms (tuples only).

## Control plane

`$XDG_RUNTIME_DIR/lynx/control.sock` accepts newline-delimited JSON, mirroring
the hyprctl keyword workflow so lynxde settings keep working unchanged:

```json
{"keyword": ["general:gaps_in", "6"]}
{"dispatch": ["closewindow"]}
{"query": "outputs"}
```

Keyword paths intentionally match Hyprland's (`general:gaps_in`,
`decoration:rounding`, `general:layout`, ...). The settings app pushes the
same settings keys either here (LWP session) or via `hyprctl keyword`
(Hyprland session).

## Compatibility matrix

| Apps                        | How they run on LWP                                  |
|-----------------------------|-------------------------------------------------------|
| lynxde components           | native LWP surfaces (`lwp_common.py`)                 |
| Wayland-native apps         | `lynx-bridge` sidecar (xdg-shell subset over shm)     |
| X11 apps                    | `Xwayland` hosted by `lynx-bridge`'s socket           |
| whole Hyprland desktop      | untouched; installer keeps Wayland/X11 session entries|

The bridge implements the registry subset such apps actually need
(`wl_compositor`, `wl_shm`, `wl_seat`, `wl_output`, `xdg_wm_base`). Clients
requesting GPU dmabuf acceleration fall back to shared memory automatically.

## Reference implementation map

| File                     | Role                                                    |
|--------------------------|---------------------------------------------------------|
| `taskbar/lwp_protocol.py`| codec, framed connections, sealed shared buffers         |
| `taskbar/lwp_layout.py`  | dwindle/master tiling, gaps, borders (pure functions)    |
| `taskbar/lwp_core.py`    | compositor state machine, input routing, damage, control |
| `taskbar/lynx_compositor.py` | daemon: epoll loop, render/present backends, sockets |
| `taskbar/lwp_kms.py`     | experimental DRM/KMS direct-scanout backend              |
| `taskbar/lwp_bridge.py`  | optional Wayland→LWP translation sidecar                 |
| `taskbar/lwp_common.py`  | client library used by lynxde components                 |

Every module carries a `--selftest` that runs headless (no Qt, no DRM needed
for protocol/layout/core tests).
