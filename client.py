import gi
import time
import socket
import struct
import threading
from collections import deque

gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

Gst.init(None)

VIDEO_PORT = 5000
META_PORT  = 5001

# ── FIFO queue chứa send_ts từ Pi ────────────────────────
send_ts_queue = deque(maxlen=150)   # buffer ~5s ở 30fps
queue_lock    = threading.Lock()

# ── Internet frame-loss tracking ─────────────────────────
# Pi nhúng meta_seq (4B) vào mỗi metadata packet (format '>QQI')
# Client so sánh seq liên tiếp để phát hiện frame bị mất trên internet
meta_lock          = threading.Lock()
_next_expected_seq = None   # seq tiếp theo ta mong nhận
_meta_received     = 0      # tổng packet metadata nhận được (lifetime)
_meta_lost         = 0      # tổng frame mất trên internet (lifetime)
# Per-second window để hiển thị %
_sec_received      = 0
_sec_lost          = 0

def meta_receiver():
    """Nhận (pts, send_ts, seq) từ Pi, push send_ts vào FIFO, cập nhật loss."""
    global _next_expected_seq, _meta_received, _meta_lost
    global _sec_received, _sec_lost

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('0.0.0.0', META_PORT))
    sock.settimeout(1.0)
    print(f"[META] Listening UDP:{META_PORT}")
    while True:
        try:
            data, _ = sock.recvfrom(64)
            # Format mới: pts(8B) + t0/send_ts(8B) + seq(4B) = 20B
            # Tương thích ngược: nếu chỉ có 16B thì không đọc seq
            if len(data) >= 20:
                _pts, send_ts, seq = struct.unpack('>QQI', data[:20])
                with meta_lock:
                    # Push send_ts để tính e2e latency
                    send_ts_queue.append(send_ts)
                    _meta_received += 1
                    _sec_received  += 1
                    # Phát hiện gap trong seq → frame mất trên internet
                    if _next_expected_seq is None:
                        _next_expected_seq = seq + 1
                    elif seq > _next_expected_seq:
                        gap = seq - _next_expected_seq
                        _meta_lost    += gap
                        _sec_lost     += gap
                        _next_expected_seq = seq + 1
                    elif seq == _next_expected_seq:
                        _next_expected_seq += 1
                    # seq < expected: trùng lặp / out-of-order, bỏ qua
            elif len(data) >= 16:
                # Tương thích bản server cũ (không có seq)
                _pts, send_ts = struct.unpack('>QQ', data[:16])
                with meta_lock:
                    send_ts_queue.append(send_ts)
        except socket.timeout:
            continue
        except Exception as e:
            print(f"[META] Error: {e}")

threading.Thread(target=meta_receiver, daemon=True).start()

# ── Stage timing ──────────────────────────────────────────
c_stage = {}
c2_seq  = 0

jitter_delays = deque(maxlen=60)
depay_delays  = deque(maxlen=60)
decode_delays = deque(maxlen=60)
rx_delays     = deque(maxlen=60)
e2e_delays    = deque(maxlen=60)

frame_count = 0
byte_count  = 0   # bytes ở decoder output (raw frame)
start_time  = time.time()

# ── Network bitrate: đếm RTP bytes tại c0 ────────────────
# c0 nằm ngay sau udpsrc → đây là lưu lượng thực tế nhận từ internet
# (bao gồm RTP header nhưng không tính UDP/IP header)
c0_byte_count = 0
c0_lock       = threading.Lock()

pipeline_str = f"""
udpsrc port={VIDEO_PORT} caps="application/x-rtp,media=video,encoding-name=H264,payload=96" !
identity name=c0 !
rtpjitterbuffer latency=30 !
identity name=c1 !
rtph264depay !
identity name=c2 !
avdec_h264 !
identity name=c3 !
autovideosink sync=false
"""
pipeline = Gst.parse_launch(pipeline_str)
c0 = pipeline.get_by_name("c0")   # sau udpsrc  – RTP packet thô từ mạng
c1 = pipeline.get_by_name("c1")   # sau jitterbuf
c2 = pipeline.get_by_name("c2")   # sau depay, 1 H264 NAL/frame
c3 = pipeline.get_by_name("c3")   # sau avdec, raw frame

# ── Probe c0: ghi r0 + đo internet bitrate ───────────────
def probe_c0(pad, info):
    global c0_byte_count
    buf = info.get_buffer()
    if not buf:
        return Gst.PadProbeReturn.OK
    # Đo bitrate internet: tích lũy byte RTP thô từ mạng
    with c0_lock:
        c0_byte_count += buf.get_size()
    # Ghi r0 (chỉ lần đầu / frame)
    seq = c2_seq
    if seq not in c_stage:
        c_stage[seq] = {"r0": time.time_ns()}
    return Gst.PadProbeReturn.OK

# ── Probe c1: ghi r1 (sau jitterbuffer) ──────────────────
def probe_c1(pad, info):
    if not info.get_buffer():
        return Gst.PadProbeReturn.OK
    seq = c2_seq
    if seq in c_stage and "r1" not in c_stage[seq]:
        c_stage[seq]["r1"] = time.time_ns()
    return Gst.PadProbeReturn.OK

# ── Probe c2: ghi r2, pop send_ts ────────────────────────
def probe_c2(pad, info):
    global c2_seq
    if not info.get_buffer():
        return Gst.PadProbeReturn.OK
    r2  = time.time_ns()
    seq = c2_seq
    send_ts = None
    with queue_lock:
        if send_ts_queue:
            send_ts = send_ts_queue.popleft()
    if seq not in c_stage:
        c_stage[seq] = {}
    c_stage[seq]["r2"]      = r2
    c_stage[seq]["send_ts"] = send_ts
    c2_seq += 1
    return Gst.PadProbeReturn.OK

# ── Probe c3: ghi r3, tính delay, đếm fps ────────────────
def probe_c3(pad, info):
    global frame_count, byte_count
    buf = info.get_buffer()
    if not buf:
        return Gst.PadProbeReturn.OK
    r3  = time.time_ns()
    seq = c2_seq - 1
    if seq in c_stage:
        d  = c_stage.pop(seq)
        r0 = d.get("r0")
        r1 = d.get("r1")
        r2 = d.get("r2")
        st = d.get("send_ts")
        if r0 and r1:
            jitter_delays.append((r1 - r0) / 1e6)
        if r1 and r2:
            depay_delays.append( (r2 - r1) / 1e6)
        if r2:
            decode_delays.append((r3 - r2) / 1e6)
        if r0:
            rx_delays.append(    (r3 - r0) / 1e6)
        if st:
            e2e_delays.append(   (r3 - st) / 1e6)
    # Dọn entry cũ hơn 5s
    cutoff = time.time_ns() - 5_000_000_000
    stale  = [k for k, v in list(c_stage.items()) if v.get("r0", cutoff+1) < cutoff]
    for k in stale:
        del c_stage[k]
    frame_count += 1
    byte_count  += buf.get_size()
    return Gst.PadProbeReturn.OK

c0.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, probe_c0)
c1.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, probe_c1)
c2.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, probe_c2)
c3.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, probe_c3)

# ── Print stats mỗi giây ─────────────────────────────────
def print_stats():
    global frame_count, byte_count, start_time
    global c0_byte_count, _sec_received, _sec_lost

    elapsed = time.time() - start_time
    fps  = frame_count / elapsed if elapsed > 0 else 0
    mbps = (byte_count * 8) / elapsed / 1e6 if elapsed > 0 else 0
    avg  = lambda d: f"{sum(d)/len(d):.2f}" if d else "-.--"

    # ── Internet bitrate (RTP bytes nhận tại c0) ──────────
    with c0_lock:
        c0_b  = c0_byte_count
        c0_byte_count = 0
    net_mbps = (c0_b * 8) / elapsed / 1e6 if elapsed > 0 else 0

    # ── Internet frame-loss (từ seq gap trong metadata) ───
    with meta_lock:
        sec_rx   = _sec_received
        sec_lost = _sec_lost
        _sec_received = 0
        _sec_lost     = 0
        total_rx   = _meta_received
        total_lost = _meta_lost

    if sec_rx + sec_lost > 0:
        loss_pct_sec = sec_lost / (sec_rx + sec_lost) * 100
    else:
        loss_pct_sec = 0.0
    if total_rx + total_lost > 0:
        loss_pct_total = total_lost / (total_rx + total_lost) * 100
    else:
        loss_pct_total = 0.0

    # ── FIFO health ────────────────────────────────────────
    with queue_lock:
        q = len(send_ts_queue)

    # ── Network delay = e2e - rx_pipeline ─────────────────
    net_str = "-.--"
    if e2e_delays and rx_delays:
        net_ms  = sum(e2e_delays)/len(e2e_delays) - sum(rx_delays)/len(rx_delays)
        net_str = f"{net_ms:.2f}"

    print(
        f"[LAPTOP]  FPS:{fps:.1f}  raw:{mbps:.2f}Mbps  (fifo:{q})\n"
        f"          net_bitrate: {net_mbps:.2f} Mbps  (RTP bytes tại c0)\n"
        f"          inet_loss  : {loss_pct_sec:.1f}%  "
        f"(1s: lost={sec_lost}/{sec_rx+sec_lost}  "
        f"total: lost={total_lost}/{total_rx+total_lost} = {loss_pct_total:.2f}%)\n"
        f"          jitter_buf : {avg(jitter_delays)} ms\n"
        f"          depay      : {avg(depay_delays)} ms\n"
        f"          decode     : {avg(decode_delays)} ms\n"
        f"          total_rx   : {avg(rx_delays)} ms\n"
        f"          network    : {net_str} ms  (e2e - total_rx)\n"
        f"          end2end    : {avg(e2e_delays)} ms\n"
    )
    frame_count = 0
    byte_count  = 0
    start_time  = time.time()
    return True

GLib.timeout_add_seconds(1, print_stats)
pipeline.set_state(Gst.State.PLAYING)
print(f"[LAPTOP] Video UDP:{VIDEO_PORT}  |  Meta UDP:{META_PORT}")
GLib.MainLoop().run()
