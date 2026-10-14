"""A QR code as SVG, with nothing to install.

The phone companion's address is long and has a secret in it; typing it on a
phone is where people give up. No QR library ships with the app, and a page
fetching one from the internet would break the "nothing leaves" promise, so
this is a small encoder: byte mode, error correction level M, versions 1–10
(up to 213 bytes — an address with its key is about 80). Standard ISO 18004:
Reed–Solomon over GF(256), the eight masks scored by the usual penalties.
"""
from __future__ import annotations

# (ecc codewords per block, [(blocks, data codewords per block), ...]) — level M
_M = {
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]),
    5: (24, [(2, 43)]), 6: (16, [(4, 27)]), 7: (18, [(4, 31)]),
    8: (22, [(2, 38), (2, 39)]), 9: (22, [(3, 36), (2, 37)]), 10: (26, [(4, 43), (1, 44)]),
}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
          7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}

_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs(data: list, n: int) -> list:
    gen = [1]
    for i in range(n):
        nxt = [0] * (len(gen) + 1)
        for j, g in enumerate(gen):
            nxt[j] ^= g
            nxt[j + 1] ^= _mul(g, _EXP[i])
        gen = nxt
    rem = list(data) + [0] * n
    for i in range(len(data)):
        c = rem[i]
        if c:
            for j in range(1, len(gen)):
                rem[i + j] ^= _mul(gen[j], c)
    return rem[len(data):]


def _bch(value: int, poly: int, bits: int) -> int:
    v = value << bits
    plen = poly.bit_length()
    while v.bit_length() >= plen:
        v ^= poly << (v.bit_length() - plen)
    return (value << bits) | v


def _codewords(data: bytes, version: int) -> list:
    ecc, groups = _M[version]
    capacity = sum(b * d for b, d in groups)
    bits = []

    def put(v, n):
        bits.extend((v >> (n - 1 - i)) & 1 for i in range(n))
    put(0b0100, 4)
    put(len(data), 8 if version < 10 else 16)
    for byte in data:
        put(byte, 8)
    put(0, min(4, capacity * 8 - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    cw = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = [0xEC, 0x11]
    while len(cw) < capacity:
        cw.append(pad[(len(cw) - len(bits) // 8) % 2])
    blocks, k = [], 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(cw[k:k + size])
            k += size
    eccs = [_rs(b, ecc) for b in blocks]
    out = []
    for i in range(max(len(b) for b in blocks)):
        out += [b[i] for b in blocks if i < len(b)]
    for i in range(ecc):
        out += [e[i] for e in eccs]
    return out


def _matrix(version: int, cw: list, mask: int):
    n = 17 + 4 * version
    m = [[None] * n for _ in range(n)]

    def finder(r, c):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                rr, cc = r + dr, c + dc
                if 0 <= rr < n and 0 <= cc < n:
                    on = (0 <= dr <= 6 and dc in (0, 6)) or (0 <= dc <= 6 and dr in (0, 6)) \
                        or (2 <= dr <= 4 and 2 <= dc <= 4)
                    m[rr][cc] = on
    finder(0, 0)
    finder(0, n - 7)
    finder(n - 7, 0)
    for i in range(8, n - 8):
        m[6][i] = m[i][6] = i % 2 == 0
    pos = _ALIGN[version]
    for r in pos:
        for c in pos:
            # only the three corners under a finder are skipped; one that
            # sits on a timing line is still drawn
            if (r == 6 and c == 6) or (r == 6 and c == pos[-1]) or (r == pos[-1] and c == 6):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    m[r + dr][c + dc] = max(abs(dr), abs(dc)) != 1
    m[n - 8][8] = True                                  # the dark module
    # reserve format and version areas
    for i in range(9):
        if m[8][i] is None:
            m[8][i] = False
        if m[i][8] is None:
            m[i][8] = False
    for i in range(8):
        if m[8][n - 1 - i] is None:
            m[8][n - 1 - i] = False
        if m[n - 1 - i][8] is None:
            m[n - 1 - i][8] = False
    if version >= 7:
        for i in range(6):
            for j in range(3):
                m[i][n - 11 + j] = False
                m[n - 11 + j][i] = False
    fixed = [[v is not None for v in row] for row in m]

    bits = []
    for c in cw:
        bits.extend((c >> (7 - i)) & 1 for i in range(8))
    k = 0
    col = n - 1
    up = True
    while col > 0:
        if col == 6:
            col -= 1
        for i in range(n):
            r = n - 1 - i if up else i
            for c in (col, col - 1):
                if not fixed[r][c]:
                    m[r][c] = bool(bits[k]) if k < len(bits) else False
                    k += 1
        up = not up
        col -= 2

    masks = [lambda r, c: (r + c) % 2 == 0, lambda r, c: r % 2 == 0, lambda r, c: c % 3 == 0,
             lambda r, c: (r + c) % 3 == 0, lambda r, c: (r // 2 + c // 3) % 2 == 0,
             lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
             lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
             lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0]
    f = masks[mask]
    for r in range(n):
        for c in range(n):
            if not fixed[r][c] and f(r, c):
                m[r][c] = not m[r][c]

    fmt = _bch((0b00 << 3) | mask, 0x537, 10) ^ 0x5412          # level M = 00
    fb = [(fmt >> i) & 1 for i in range(15)]                     # bit 0 first
    for i in range(6):
        m[i][8] = bool(fb[i])
    m[7][8] = bool(fb[6])
    m[8][8] = bool(fb[7])
    m[8][7] = bool(fb[8])
    for i in range(9, 15):
        m[8][14 - i] = bool(fb[i])
    for i in range(8):
        m[8][n - 1 - i] = bool(fb[i])
    for i in range(8, 15):
        m[n - 15 + i][8] = bool(fb[i])
    m[n - 8][8] = True
    if version >= 7:
        vi = _bch(version, 0x1F25, 12)
        for i in range(18):
            bit = bool((vi >> i) & 1)
            a, b = i // 3, i % 3
            m[a][n - 11 + b] = bit
            m[n - 11 + b][a] = bit
    return m


def _penalty(m) -> int:
    n = len(m)
    p = 0
    for grid in (m, [list(x) for x in zip(*m)]):
        for row in grid:
            run, last = 0, None
            for v in row:
                if v == last:
                    run += 1
                else:
                    if run >= 5:
                        p += run - 2
                    run, last = 1, v
            if run >= 5:
                p += run - 2
            s = "".join("1" if v else "0" for v in row)
            p += 40 * (s.count("10111010000") + s.count("00001011101"))
    for r in range(n - 1):
        for c in range(n - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                p += 3
    dark = sum(v for row in m for v in row)
    p += 10 * (abs(dark * 20 - n * n * 10) // (n * n))
    return p


def matrix(text: str):
    data = text.encode("utf-8")
    for v in range(1, 11):
        ecc, groups = _M[v]
        cap = sum(b * d for b, d in groups)
        overhead = 4 + (8 if v < 10 else 16)
        if len(data) * 8 + overhead <= cap * 8:
            cw = _codewords(data, v)
            return min((_matrix(v, cw, k) for k in range(8)), key=_penalty)
    raise ValueError("too long for a QR code this encoder makes (213 bytes)")


def svg(text: str, scale: int = 6, border: int = 4) -> str:
    m = matrix(text)
    n = len(m)
    size = (n + 2 * border) * scale
    path = "".join(f"M{(c + border) * scale},{(r + border) * scale}h{scale}v{scale}h-{scale}z"
                   for r in range(n) for c in range(n) if m[r][c])
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
            f'width="{size}" height="{size}" shape-rendering="crispEdges">'
            f'<rect width="100%" height="100%" fill="#fff"/><path d="{path}" fill="#000"/></svg>')
