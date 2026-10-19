"""Minimal XMEye / Xiongmai (DVRIP "Sofia", TCP 34567) client.

Protocol logic follows OpenIPC python-dvr (dvrip.py / NVRVideoDownloader.py),
rewritten so a download can be streamed to disk with progress and cancelled.
"""

import hashlib
import json
import logging
import os
import socket
import struct
import time

log = logging.getLogger("xmeye")

HEADER = struct.Struct("<BB2xII2xHI")  # head, version, session, seq, msgid, length

LOGIN = 1000
KEEPALIVE = 1006
GET_INFO = 1042
SYSINFO = 1020
PLAYBACK = 1420
PLAYBACK_CLAIM = 1424
FILE_QUERY = 1440

OK_CODES = (100, 150, 515)
ERRORS = {
    101: "неизвестная ошибка",
    102: "не поддерживается",
    103: "запрос не разрешён",
    104: "пользователь уже залогинен",
    105: "пользователь не залогинен",
    106: "неверный логин или пароль",
    107: "нет прав",
    108: "таймаут",
    109: "файл не найден",
    119: "файл не найден",
    202: "пользователь не залогинен",
    203: "неверный пароль",
    204: "такого пользователя нет",
    205: "IP заблокирован после неверных паролей - подожди ~30 мин или перезагрузи регистратор",
    206: "пользователь в чёрном списке",
    207: "в чёрном списке",
    212: "аккаунт уже используется",
}


class DVRError(Exception):
    pass


class Cancelled(Exception):
    pass


def sofia_hash(password):
    md5 = hashlib.md5(password.encode("utf-8")).digest()
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    return "".join(chars[(a + b) % 62] for a, b in zip(md5[::2], md5[1::2]))


def _try_json(payload):
    """Parse a DVR JSON reply; None if the payload is not JSON (e.g. video data)."""
    raw = bytes(payload).strip(b"\x00\n\r\t ")
    if not raw.startswith(b"{"):
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace"), strict=False)
    except ValueError:
        return None


def _peek(payload, n=48):
    return bytes(payload[:n])


class DVRClient:
    def __init__(self, ip, user="admin", password="", port=34567, timeout=15):
        self.ip = ip
        self.port = port
        self.user = user
        self.password = password
        self.timeout = timeout
        self.sock = None
        self.session = 0
        self.seq = 0
        self.alive_interval = 20
        self.last_activity = 0.0

    # ---- low level -------------------------------------------------------

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self.sock.recv(min(n - len(buf), 262144))
            except socket.timeout:
                raise DVRError("регистратор не отвечает (таймаут)")
            if not chunk:
                raise DVRError("регистратор закрыл соединение")
            buf.extend(chunk)
        return buf

    def _send_packet(self, msgid, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\x0a\x00"
        pkt = HEADER.pack(0xFF, 0, self.session, self.seq, msgid, len(data)) + data
        self.sock.sendall(pkt)
        self.seq += 1
        self.last_activity = time.monotonic()

    def _recv_packet(self):
        head, _ver, _session, _seq, msgid, length = HEADER.unpack(self._recv_exact(HEADER.size))
        if head != 0xFF:
            raise DVRError("неожиданный ответ (это точно XMEye-регистратор?)")
        payload = self._recv_exact(length) if length else bytearray()
        return msgid, payload

    def request_raw(self, msgid, obj):
        """Send a request; returns (reply_msgid, payload, parsed_json_or_None)."""
        self._send_packet(msgid, obj)
        rid, payload = self._recv_packet()
        log.debug("req %d -> msg %d len %d: %r", msgid, rid, len(payload), _peek(payload, 300))
        return rid, payload, _try_json(payload)

    def request(self, msgid, obj):
        rid, payload, reply = self.request_raw(msgid, obj)
        if reply is None:
            raise DVRError(f"непонятный ответ на запрос {msgid} (msg {rid}, {len(payload)} байт): "
                           f"{_peek(payload)!r}")
        return reply

    def _sid(self):
        return "0x%08X" % self.session

    # ---- session ---------------------------------------------------------

    def connect(self):
        self.close()
        self.session = 0
        self.seq = 0
        try:
            self.sock = socket.create_connection((self.ip, self.port), timeout=self.timeout)
        except OSError as e:
            raise DVRError(f"не удалось подключиться к {self.ip}:{self.port} ({e})")
        self.sock.settimeout(self.timeout)
        reply = self.request(LOGIN, {
            "EncryptType": "MD5",
            "LoginType": "DVRIP-Web",
            "PassWord": sofia_hash(self.password),
            "UserName": self.user,
        })
        ret = reply.get("Ret")
        if ret not in OK_CODES:
            self.close()
            raise DVRError(f"логин не удался: [{ret}] {ERRORS.get(ret, '')}")
        self.session = int(reply["SessionID"], 16)
        self.alive_interval = int(reply.get("AliveInterval", 20) or 20)
        return reply

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def keepalive_if_needed(self):
        if time.monotonic() - self.last_activity > self.alive_interval / 2:
            self.request(KEEPALIVE, {"Name": "KeepAlive", "SessionID": self._sid()})

    def get_info(self, name, msgid=GET_INFO):
        reply = self.request(msgid, {"Name": name, "SessionID": self._sid()})
        if reply.get("Ret") in OK_CODES and name in reply:
            return reply[name]
        return None

    def channel_count(self):
        info = self.get_info("SystemInfo", SYSINFO) or {}
        return int(info.get("VideoInChannel", 0) or 0) + int(info.get("DigChannel", 0) or 0)

    # ---- files -----------------------------------------------------------

    def list_files(self, begin, end, channel=0, filetype="h264"):
        """begin/end: 'YYYY-MM-DD HH:MM:SS'. Returns list of file dicts."""
        result, seen = [], set()
        start = begin
        while True:
            reply = self.request(FILE_QUERY, {
                "Name": "OPFileQuery",
                "OPFileQuery": {
                    "BeginTime": start,
                    "Channel": channel,
                    "DriverTypeMask": "0x0000FFFF",
                    "EndTime": end,
                    "Event": "*",
                    "StreamType": "0x00000000",
                    "Type": filetype,
                },
            })
            items = (reply.get("OPFileQuery") or []) if reply.get("Ret") == 100 else []
            new = [f for f in items if f.get("FileName") not in seen]
            for f in new:
                seen.add(f["FileName"])
                result.append(f)
            # the DVR returns at most 64 entries per query: page by the last BeginTime
            if len(items) < 64 or not new:
                break
            start = items[-1]["BeginTime"]
        result.sort(key=lambda f: (f["BeginTime"], f["FileName"]))
        return result

    def _playback(self, action, f):
        return {
            "Name": "OPPlayBack",
            "OPPlayBack": {
                "Action": action,
                "Parameter": {
                    "PlayMode": "ByName",
                    "FileName": f["FileName"],
                    "StreamType": 0,
                    "Value": 0,
                    "TransMode": "TCP",
                },
                "StartTime": f["BeginTime"],
                "EndTime": f["EndTime"],
            },
        }

    def download(self, f, out, progress=None, cancel=None, expected=0):
        """Stream file `f` (dict from list_files) into binary stream `out`.

        progress(bytes_received) is called periodically; cancel() -> True aborts.
        expected: approximate size in bytes, used when the DVR ends the stream
        without the zero-length end marker. Returns number of bytes written.
        """
        # Some firmwares answer Claim/DownloadStart with JSON, others start sending
        # video right away (python-dvr writes that first reply into the file as-is).
        received = 0
        _rid, _payload, reply = self.request_raw(PLAYBACK_CLAIM, self._playback("Claim", f))
        if reply is not None and reply.get("Ret") not in OK_CODES:
            raise DVRError(f"Claim отклонён: [{reply.get('Ret')}] {ERRORS.get(reply.get('Ret'), '')}")
        _rid, payload, reply = self.request_raw(PLAYBACK, self._playback("DownloadStart", f))
        if reply is not None:
            if reply.get("Ret") not in OK_CODES:
                raise DVRError(f"DownloadStart отклонён: [{reply.get('Ret')}] {ERRORS.get(reply.get('Ret'), '')}")
        elif payload:
            out.write(payload)
            received += len(payload)

        last_report = 0.0
        logged = 0
        while True:
            if cancel and cancel():
                # connection is mid-stream; caller must reconnect
                self.close()
                raise Cancelled()
            try:
                msgid, payload = self._recv_packet()
            except DVRError:
                if expected and received >= expected * 0.95:
                    log.debug("stream ended without marker at %d/%d bytes", received, expected)
                    self.close()  # session state unknown, reconnect for the next file
                    break
                raise
            if logged < 3:
                logged += 1
                log.debug("data msg %d len %d: %r", msgid, len(payload), _peek(payload, 32))
            if not payload:
                log.debug("end marker msg %d after %d bytes", msgid, received)
                break
            out.write(payload)
            received += len(payload)
            now = time.monotonic()
            if progress and now - last_report > 0.2:
                last_report = now
                progress(received)
        self.last_activity = time.monotonic()
        if progress:
            progress(received)
        if self.sock:
            try:
                self.request_raw(PLAYBACK, self._playback("DownloadStop", f))
            except DVRError:
                self.close()
        return received


# ---- Xiongmai private stream (.h264 files from the DVR) ------------------------
#
# 00 00 01 FC  I-frame : 16-byte header [sync][codec][fps][width/8][height/8][time:4][len:4]
# 00 00 01 FD  P-frame :  8-byte header [sync][len:4]
# 00 00 01 FE  picture : 16-byte header, same layout as FC
# 00 00 01 FA  audio   :  8-byte header [sync][codec][rate][len:2]
# 00 00 01 F9  info    :  8-byte header [sync][type][?][len:2]

FRAME_HEADER = {0xFC: 16, 0xFE: 16, 0xFD: 8, 0xFA: 8, 0xF9: 8}
VIDEO_CODECS = {2: "h264", 3: "hevc"}
MAX_FRAME = 16 << 20


def iter_private_frames(fh):
    """Yield (type, header, payload) from a Xiongmai private stream file object."""
    buf = bytearray()
    start = 0
    eof = False

    def need(n):
        nonlocal buf, start, eof
        while len(buf) - start < n and not eof:
            chunk = fh.read(1 << 20)
            if not chunk:
                eof = True
                break
            if start:
                del buf[:start]
                start = 0
            buf += chunk
        return len(buf) - start >= n

    while need(8):
        t = buf[start + 3]
        if buf[start:start + 3] != b"\x00\x00\x01" or t not in FRAME_HEADER:
            j = buf.find(b"\x00\x00\x01", start + 1)
            start = j if j >= 0 else max(len(buf) - 3, start + 1)
            continue
        hl = FRAME_HEADER[t]
        if not need(hl):
            return
        if hl == 16:
            n = int.from_bytes(buf[start + 12:start + 16], "little")
        elif t == 0xFD:
            n = int.from_bytes(buf[start + 4:start + 8], "little")
        else:
            n = int.from_bytes(buf[start + 6:start + 8], "little")
        if n > MAX_FRAME:          # garbage that happened to look like a header
            start += 1
            continue
        if not need(hl + n):       # truncated last frame
            yield t, bytes(buf[start:start + hl]), bytes(buf[start + hl:])
            return
        yield t, bytes(buf[start:start + hl]), bytes(buf[start + hl:start + hl + n])
        start += hl + n


def to_mp4(src, dst, ffmpeg):
    """Strip Xiongmai frame headers and remux the video into MP4 (no re-encoding).

    Returns dict with codec/fps/size info. Raises RuntimeError on failure.
    """
    import subprocess
    import tempfile

    proc = None
    info = {}
    written = 0
    with open(src, "rb") as fh, tempfile.TemporaryFile() as err:
        try:
            for t, hdr, payload in iter_private_frames(fh):
                if proc is None:
                    if t != 0xFC:
                        continue           # P-frames before the first I-frame are undecodable
                    codec = VIDEO_CODECS.get(hdr[4])
                    if not codec:
                        raise RuntimeError(f"неизвестный кодек {hdr[4]}")
                    info = {"codec": codec, "fps": hdr[5] or 25, "width": hdr[6] * 8, "height": hdr[7] * 8}
                    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                           "-f", codec, "-framerate", str(info["fps"]), "-i", "pipe:0",
                           "-c", "copy", "-movflags", "+faststart"]
                    if codec == "hevc":
                        cmd += ["-tag:v", "hvc1"]  # lets Windows/QuickTime players open it
                    proc = subprocess.Popen(cmd + [str(dst)], stdin=subprocess.PIPE,
                                            stdout=subprocess.DEVNULL, stderr=err,
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if t in (0xFC, 0xFD):
                    proc.stdin.write(payload)
                    written += len(payload)
        except BrokenPipeError:
            pass
        finally:
            if proc:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
                proc.wait()
        if proc is None:
            raise RuntimeError("в файле нет ни одного I-кадра")
        err.seek(0)
        msg = err.read().decode("utf-8", errors="replace").strip().splitlines()
    if proc.returncode != 0:
        raise RuntimeError(msg[-1] if msg else f"ffmpeg код {proc.returncode}")
    size = os.path.getsize(dst) if os.path.exists(dst) else 0
    if size < written * 0.9:
        raise RuntimeError(f"MP4 подозрительно маленький ({size} из {written} байт)")
    info["video_bytes"] = written
    return info
