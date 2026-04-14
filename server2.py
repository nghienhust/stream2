import gi
import time
import socket
import struct
import psutil
from collections import deque

gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

Gst.init(None)

LAPTOP_IP  = "192.168.88.155"
VIDEO_PORT = 5000
META_PORT  = 5001

meta_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# Stage timing keyed by buf.pts (ns, unique per frame)
stage_ts = {}

enc_delays  = deque(maxlen=60)
pack_delays = deque(maxlen=60)
tx_delays   = deque(maxlen=60)

frame_count = 0   # fps per-second counter (reset mỗi giây)
byte_count  = 0
start_time  = time.time()

# ── Packet loss / throughput tracking ────────────────────
meta_seq     = 0   # sequence number gắn vào metadata packet
total_sent   = 0   # lifetime: tổng frame đã gửi

# Capture-loss: capture_total (s0) vs sent_total (s2)
# Chênh lệch = frame bị drop trong pipeline Pi (encode / buffer overflow)
capture_total = 0  # tổng frame từ v4l2src (lifetime)
sent_total    = 0  # tổng frame đã pack & gửi RTP thành công (lifetime)

# Per-second counters để hiện thị loss trong cửa sổ 1s
_cap_sec  = 0   # capture trong giây hiện tại
_sent_sec = 0   # sent trong giây hiện tại

pipeline_str = f"""
v4l2src device=/dev/video0 io-mode=2 !
video/x-h264,width=1280,height=720,framerate=30/1 !
identity name=s0 !
h264parse config-interval=1 !
identity name=s1 !
rtph264pay pt=96 !
identity name=s2 !
udpsink host={LAPTOP_IP} port={VIDEO_PORT} sync=false
"""

pipeline = Gst.parse_launch(pipeline_str)
s0 = pipeline.get_by_name("s0")
s1 = pipeline.get_by_name("s1")
s2 = pipeline.get_by_name("s2")

# ── Probe s0: ghi t0, gửi metadata (pts + t0 + seq) ─────
def probe_s0(pad, info):
    global meta_seq, total_sent, capture_total, _cap_sec
    buf = info.get_buffer()
    if not buf:
        return Gst.PadProbeReturn.OK

    pts = buf.pts
    if pts == Gst.CLOCK_TIME_NONE:
        return Gst.PadProbeReturn.OK

    t0 = time.time_ns()
    stage_ts[pts] = {"t0": t0, "packed": False}

    # Metadata mở rộng: pts(8B) + t0(8B) + seq(4B) = 20B
    # Client dùng seq để detect gap (frame loss trên internet)
    try:
        meta_sock.sendto(
            struct.pack('>QQI', pts, t0, meta_seq),
            (LAPTOP_IP, META_PORT)
        )
    except OSError:
        pass

    meta_seq      += 1
    total_sent    += 1
    capture_total += 1
    _cap_sec      += 1
    return Gst.PadProbeReturn.OK

# ── Probe s1: ghi t1 ─────────────────────────────────────
def probe_s1(pad, info):
    buf = info.get_buffer()
    if not buf:
        return Gst.PadProbeReturn.OK
    pts = buf.pts
    if pts != Gst.CLOCK_TIME_NONE and pts in stage_ts:
        if "t1" not in stage_ts[pts]:
            stage_ts[pts]["t1"] = time.time_ns()
    return Gst.PadProbeReturn.OK

# ── Probe s2: ghi t2, tính delay, đếm bytes ──────────────
# rtph264pay tạo nhiều RTP packet / I-frame → probe gọi nhiều lần cùng pts
# Kiểm tra flag "packed" để chỉ tính delay 1 lần / frame
def probe_s2(pad, info):
    global frame_count, byte_count, sent_total, _sent_sec
    buf = info.get_buffer()
    if not buf:
        return Gst.PadProbeReturn.OK
    pts = buf.pts
    byte_count += buf.get_size()

    if pts != Gst.CLOCK_TIME_NONE and pts in stage_ts:
        d = stage_ts[pts]
        if not d["packed"] and "t1" in d:
            t2 = time.time_ns()
            d["packed"] = True
            enc_delays.append( (d["t1"] - d["t0"]) / 1e6 )
            pack_delays.append((t2      - d["t1"]) / 1e6 )
            tx_delays.append(  (t2      - d["t0"]) / 1e6 )
            frame_count += 1
            sent_total  += 1   # frame đã pack thành công
            _sent_sec   += 1
            del stage_ts[pts]

    # Dọn entry cũ hơn 5s
    now_ns = time.time_ns()
    stale  = [k for k, v in list(stage_ts.items())
              if now_ns - v["t0"] > 5_000_000_000]
    for k in stale:
        del stage_ts[k]
    return Gst.PadProbeReturn.OK

s0.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, probe_s0)
s1.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, probe_s1)
s2.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, probe_s2)

# ── Stats in/out ─────────────────────────────────────────
def print_stats():
    global frame_count, byte_count, start_time, _cap_sec, _sent_sec
    elapsed = time.time() - start_time
    fps  = frame_count / elapsed if elapsed > 0 else 0
    mbps = (byte_count * 8) / elapsed / 1e6 if elapsed > 0 else 0
    cpu  = psutil.cpu_percent()
    avg  = lambda d: f"{sum(d)/len(d):.2f}" if d else "-.--"

    # Capture loss trong cửa sổ 1 giây vừa rồi
    # _cap_sec = frames ra từ v4l2src
    # _sent_sec = frames thực sự được pack & gửi
    # Chênh lệch = drop trong h264parse / rtph264pay / buffer
    if _cap_sec > 0:
        cap_loss_pct = (_cap_sec - _sent_sec) / _cap_sec * 100
    else:
        cap_loss_pct = 0.0

    print(
        f"[PI]  FPS:{fps:.1f}  {mbps:.2f}Mbps  CPU:{cpu:.1f}%\n"
        f"       encode    : {avg(enc_delays)} ms  (h264parse)\n"
        f"       pack      : {avg(pack_delays)} ms  (rtph264pay)\n"
        f"       total_tx  : {avg(tx_delays)} ms  (server)\n"
        f"       cap_loss  : {cap_loss_pct:.1f}%  "
        f"(cap:{_cap_sec} sent:{_sent_sec} / 1s  total_sent:{total_sent})\n"
    )

    frame_count = 0
    byte_count  = 0
    _cap_sec    = 0
    _sent_sec   = 0
    start_time  = time.time()
    return True

GLib.timeout_add_seconds(1, print_stats)
pipeline.set_state(Gst.State.PLAYING)
print(f"[PI] Video   {LAPTOP_IP}:{VIDEO_PORT}")
print(f"[PI] Meta    {LAPTOP_IP}:{META_PORT}  (20B/frame: pts+t0+seq)")
GLib.MainLoop().run()
