"""Minimal WebSocket (RFC 6455) server and client on asyncio, standard library only.

Enough for the classroom hub: text frames, fragmentation, ping/pong, close, client masking. No extensions
(no permessage-deflate), no subprotocols. Kept dependency-free so the hub image is python:slim plus three files.
"""
import asyncio
import base64
import hashlib
import os
import struct
from urllib.parse import parse_qs, urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 64 * 1024 * 1024   # a /map from rosbridge as JSON can be several MB


class Closed(Exception):
    """the other side closed the connection (or it broke)"""


def _xor(data: bytes, key: bytes) -> bytes:
    n = len(data)
    if not n:
        return data
    k = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(k, "big")).to_bytes(n, "big")


class WS:
    """one WebSocket connection; `mask` = we are the client (client frames must be masked)"""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, mask: bool,
                 path: str = "", query: dict | None = None, peer: str = ""):
        self.r, self.w, self.mask = reader, writer, mask
        self.path, self.query, self.peer = path, query or {}, peer
        self.closed = False
        self._lock = asyncio.Lock()

    async def send(self, text: str) -> None:
        await self._frame(0x1, text.encode("utf-8"))

    async def _frame(self, op: int, data: bytes) -> None:
        if self.closed:
            raise Closed()
        n = len(data)
        bit = 0x80 if self.mask else 0
        hdr = bytearray([0x80 | op])
        if n < 126:
            hdr.append(bit | n)
        elif n < 65536:
            hdr.append(bit | 126)
            hdr += struct.pack("!H", n)
        else:
            hdr.append(bit | 127)
            hdr += struct.pack("!Q", n)
        if self.mask:
            key = os.urandom(4)
            hdr += key
            data = _xor(data, key)
        try:
            async with self._lock:
                self.w.write(bytes(hdr) + data)
                await self.w.drain()
        except (ConnectionError, RuntimeError) as e:
            self.closed = True
            raise Closed() from e

    async def recv(self) -> str:
        """the next text (or binary, decoded) message; answers pings; raises Closed"""
        buf = b""
        try:
            while True:
                h = await self.r.readexactly(2)
                fin, op = h[0] & 0x80, h[0] & 0x0F
                masked, n = h[1] & 0x80, h[1] & 0x7F
                if n == 126:
                    n = struct.unpack("!H", await self.r.readexactly(2))[0]
                elif n == 127:
                    n = struct.unpack("!Q", await self.r.readexactly(8))[0]
                if n + len(buf) > MAX_MESSAGE:
                    raise Closed("message too big")
                key = await self.r.readexactly(4) if masked else None
                data = await self.r.readexactly(n)
                if key:
                    data = _xor(data, key)
                if op == 0x8:                                   # close
                    try:
                        await self._frame(0x8, data[:2])
                    except Closed:
                        pass
                    self._drop()
                    raise Closed()
                if op == 0x9:                                   # ping
                    await self._frame(0xA, data)
                    continue
                if op == 0xA:                                   # pong
                    continue
                buf += data
                if fin:
                    return buf.decode("utf-8", "replace")
        except (asyncio.IncompleteReadError, ConnectionError) as e:
            self._drop()
            raise Closed() from e

    async def close(self, code: int = 1000) -> None:
        if self.closed:
            return
        try:
            await self._frame(0x8, struct.pack("!H", code))
        except Closed:
            pass
        self._drop()

    def _drop(self) -> None:
        self.closed = True
        try:
            self.w.close()
        except Exception:   # noqa: BLE001 - already gone
            pass


async def serve(handler, host: str, port: int) -> asyncio.base_events.Server:
    """accept WebSocket connections and run `await handler(ws)` for each; plain GET / answers 200 (health check)"""

    async def conn(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 10)
        except Exception:   # noqa: BLE001 - not HTTP, too slow or too big
            w.close()
            return
        lines = raw.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        target = parts[1] if len(parts) > 1 else "/"
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        key = headers.get("sec-websocket-key")
        if "websocket" not in headers.get("upgrade", "").lower() or not key:
            body = b"mtrobo classroom hub\n"
            w.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
            await w.drain()
            w.close()
            return
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        w.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                 f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        await w.drain()
        u = urlsplit(target)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        peer = w.get_extra_info("peername")
        ws = WS(r, w, mask=False, path=u.path, query=q, peer=str(peer[0]) if peer else "")
        try:
            await handler(ws)
        except Closed:
            pass
        finally:
            await ws.close()

    return await asyncio.start_server(conn, host, port, limit=2 ** 16)


async def connect(url: str, timeout: float = 5.0) -> WS:
    """open a client WebSocket to ws://host:port/path?query"""
    u = urlsplit(url)
    r, w = await asyncio.wait_for(asyncio.open_connection(u.hostname, u.port or 80, limit=2 ** 16), timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    w.write((f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port or 80}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
             f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    await w.drain()
    resp = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), timeout)
    if b" 101 " not in resp.split(b"\r\n", 1)[0]:
        w.close()
        raise Closed(f"no upgrade from {url}")
    return WS(r, w, mask=True, path=u.path)
