# -*- coding: utf-8 -*-
"""cdp.py —— 最小 CDP 客户端（明文 ws://，够用即可）。"""
import base64
import json
import os
import socket
import struct


class CDP:
    def __init__(self, ws_url, timeout=20):
        assert ws_url.startswith('ws://'), ws_url
        rest = ws_url[5:]
        host_port, path = rest.split('/', 1)
        host, port = host_port.rsplit(':', 1)
        self.s = socket.create_connection((host, int(port)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ('GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n'
               'Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n'
               'Sec-WebSocket-Version: 13\r\n\r\n' % (path, host_port, key))
        try:
            self.s.sendall(req.encode())
            buf = b''
            while b'\r\n\r\n' not in buf:
                chunk = self.s.recv(4096)
                if not chunk:
                    raise IOError('ws handshake closed')
                buf += chunk
        except Exception:
            # 握手失败要关掉，否则每点一次「我已完成登录」漏一个 socket
            try:
                self.s.close()
            except Exception:
                pass
            raise
        self._id = 0
        self._buf = b''

    def _read(self, n):
        out = b''
        while len(out) < n:
            c = self.s.recv(n - len(out))
            if not c:
                raise IOError('ws closed')
            out += c
        return out

    def send(self, obj):
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(data)
        hdr = bytes([0x81])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack('>H', n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack('>Q', n)
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self):
        # 非数据帧（Close/Ping/Pong）要连长度和负载一起读掉，只丢 2 字节头会让整条
        # 流永久错位，后面 json.loads 直接抛 ValueError 或一路卡到超时。
        while True:
            h = self._read(2)
            op = h[0] & 0x0F
            ln = h[1] & 0x7F
            if ln == 126:
                ln = struct.unpack('>H', self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack('>Q', self._read(8))[0]
            if h[1] & 0x80:          # 服务端到客户端理论上不掩码，兜底吃掉
                self._read(4)
            if op in (0x1, 0x2):
                break
            if op == 0x8:
                raise IOError('ws closed by peer')
            if ln:
                self._read(ln)
        return json.loads(self._read(ln).decode('utf-8', 'replace'))

    def call(self, method, params=None, timeout_msgs=20000):
        self._id += 1
        mid = self._id
        self.send({'id': mid, 'method': method, 'params': params or {}})
        for _ in range(timeout_msgs):
            m = self.recv()
            if m.get('id') == mid:
                if 'error' in m:
                    raise RuntimeError('%s: %s' % (method, m['error']))
                return m.get('result', {})
        raise TimeoutError(method)

    def fire(self, method, params=None):
        """只发不等回包 —— reload/navigate 这类会让页面卸载的调用必须用它，
        否则事件洪泛会让 call() 永远等不到自己的 id。"""
        self._id += 1
        self.send({"id": self._id, "method": method, "params": params or {}})
        return self._id

    def eval(self, expr, by_value=True):
        r = self.call('Runtime.evaluate', {
            'expression': expr, 'returnByValue': by_value, 'awaitPromise': True})
        res = r.get('result', {})
        if res.get('subtype') == 'error' or r.get('exceptionDetails'):
            return {'__error': (r.get('exceptionDetails') or {}).get('text',
                    res.get('description'))}
        return res.get('value')

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass
