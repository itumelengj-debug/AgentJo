"""Privacy shield — keep sensitive data away from cloud engines.

Pattern-based detection of high-signal sensitive data, tuned for a South
African / POPIA context, with two protection modes (config.PRIVACY_MODE):

  • "mask"  — detected values are replaced with stable placeholders like
    ⟦EMAIL_1⟧ before the text reaches ANY engine, and restored in what you see.
    The mapping is per-conversation and consistent across turns, and the swap
    happens at the tool boundary too: the model works with placeholders, tools
    and replies work with real values.
  • "local" — a turn containing sensitive data is routed entirely to the LOCAL
    model; nothing leaves the machine. Falls back to masking when no local
    model is running.
  • "off"   — default; behaviour unchanged.

Detectors (deliberately high-precision, checksum-validated where possible):
  SA ID numbers (13 digits, Luhn-validated) · card numbers (Luhn) · email
  addresses · SA phone numbers · common API-key/secret shapes and
  password=/token= assignments.

Honest scope: this is pattern matching, not NLP — names, addresses and other
free-text PII are NOT detected. It is a strong guard against the classic
leaks (IDs, cards, contacts, credentials), not a certification of anonymity.
"""
from __future__ import annotations

import re
import threading

PLACE_L, PLACE_R = "\u27e6", "\u27e7"          # ⟦ ⟧ — never in normal text
_PLACEHOLDER = re.compile(r"\u27e6([A-Z_]+)_(\d+)\u27e7")

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_SA_ID = re.compile(r"\b\d{13}\b")
_PHONE = re.compile(r"(?:\+27[ -]?|\b0)\d{2}[ -]?\d{3}[ -]?\d{4}\b")
_SECRET = re.compile(
    r"\b(sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16})\b")
_KV_SECRET = re.compile(
    r"\b(?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*([^\s'\"]{6,})",
    re.IGNORECASE)


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def detect(text: str) -> list:
    """[(start, end, KIND, value)] — non-overlapping, longest-first wins."""
    if not text:
        return []
    spans = []
    for m in _EMAIL.finditer(text):
        spans.append((m.start(), m.end(), "EMAIL", m.group(0)))
    for m in _SECRET.finditer(text):
        spans.append((m.start(), m.end(), "SECRET", m.group(1)))
    for m in _KV_SECRET.finditer(text):
        spans.append((m.start(1), m.end(1), "SECRET", m.group(1)))
    for m in _CARD.finditer(text):
        digits = re.sub(r"[ -]", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits) and len(digits) != 13:
            spans.append((m.start(), m.end(), "CARD", m.group(0)))
    for m in _SA_ID.finditer(text):
        if _luhn_ok(m.group(0)):
            spans.append((m.start(), m.end(), "SA_ID", m.group(0)))
    for m in _PHONE.finditer(text):
        spans.append((m.start(), m.end(), "PHONE", m.group(0)))
    # resolve overlaps: keep longer spans, then earlier ones
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    out, taken_until = [], -1
    for s in spans:
        if s[0] >= taken_until:
            out.append(s)
            taken_until = s[1]
    return out


class Masker:
    """Per-conversation, thread-safe, stable value↔placeholder mapping."""

    def __init__(self):
        self._fwd: dict[str, str] = {}          # value -> placeholder
        self._rev: dict[str, str] = {}          # placeholder -> value
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def _placeholder_for(self, kind: str, value: str) -> str:
        with self._lock:
            if value in self._fwd:
                return self._fwd[value]
            self._counts[kind] = self._counts.get(kind, 0) + 1
            ph = f"{PLACE_L}{kind}_{self._counts[kind]}{PLACE_R}"
            self._fwd[value] = ph
            self._rev[ph] = value
            return ph

    def mask(self, text: str) -> str:
        if not text:
            return text
        spans = detect(text)
        # also re-mask known values the detectors might miss in odd contexts
        if not spans and not any(v in text for v in self._fwd):
            return text
        out, pos = [], 0
        for start, end, kind, value in spans:
            out.append(text[pos:start])
            out.append(self._placeholder_for(kind, value))
            pos = end
        out.append(text[pos:])
        result = "".join(out)
        for value, ph in list(self._fwd.items()):
            if value in result:
                result = result.replace(value, ph)
        return result

    def unmask(self, text: str) -> str:
        if not text or PLACE_L not in text:
            return text
        for ph, value in self._rev.items():
            if ph in text:
                text = text.replace(ph, value)
        return text

    def deep_unmask(self, obj):
        """Restore real values inside tool inputs (dict/list/str)."""
        if isinstance(obj, str):
            return self.unmask(obj)
        if isinstance(obj, dict):
            return {k: self.deep_unmask(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.deep_unmask(v) for v in obj]
        return obj

    @property
    def mapped(self) -> int:
        return len(self._fwd)


class StreamUnmasker:
    """Wrap a streaming on_text callback so placeholders split across chunks
    are still restored: holds back only an unterminated ⟦…, flushes the rest."""

    def __init__(self, on_text, masker: Masker):
        self._on_text = on_text
        self._m = masker
        self._buf = ""

    def feed(self, chunk: str):
        self._buf += chunk
        cut = len(self._buf)
        i = self._buf.rfind(PLACE_L)
        if i != -1 and PLACE_R not in self._buf[i:]:
            # a placeholder might still be arriving — hold it back, unless it's
            # clearly just a stray bracket
            if len(self._buf) - i <= 40:
                cut = i
        if cut:
            self._on_text(self._m.unmask(self._buf[:cut]))
            self._buf = self._buf[cut:]

    def flush(self):
        if self._buf:
            self._on_text(self._m.unmask(self._buf))
            self._buf = ""


_maskers: dict[str, Masker] = {}
_mlock = threading.Lock()


def get_masker(session_id: str) -> Masker:
    key = session_id or "_default"
    with _mlock:
        if key not in _maskers:
            _maskers[key] = Masker()
        return _maskers[key]
