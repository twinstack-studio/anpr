"""Pakistani plate text clean-up and multi-frame voting."""
import re
from collections import defaultdict

# OCR confusions: in the letter part digits are really letters, and the other way round.
TO_LETTER = {"0": "O", "1": "I", "2": "Z", "3": "B", "4": "A", "5": "S", "6": "G", "7": "T", "8": "B"}
TO_DIGIT = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "G": "6", "B": "8", "T": "7"}

PLATE_RE = re.compile(r"^[A-Z]{2,4}\d{1,6}[A-Z]?$")


def normalize(raw: str) -> tuple[str, bool]:
    """Turn raw OCR text into Pakistani style: LEB-1234, LEA-20-4060, LE-16-5471A.
    Layout: 2-4 letters, optional 2-digit registration year, 1-4 digits, optional letter.
    Returns (text, looks_valid)."""
    s = re.sub(r"[^A-Z0-9]", "", raw.upper())
    if len(s) < 3:
        return s, False
    best = None
    for k in (2, 3, 4):
        for suffix in (0, 1):
            n_mid = len(s) - k - suffix
            if n_mid < 1:
                continue
            head, mid, tail = s[:k], s[k : k + n_mid], s[k + n_mid :]
            cost = sum(not c.isalpha() for c in head) + sum(not c.isdigit() for c in mid)
            cost += sum(not c.isalpha() for c in tail) * 1.5 + 0.4 * suffix + 0.8 * (tail in ("O", "Q", "D", "I"))
            # most plates: 3 letters, 3-4 digits (5-6 with the year printed in)
            cost += 0.3 * (k != 3) + 0.6 * (n_mid in (1, 2)) + 1.5 * (n_mid > 6)
            if best is None or cost < best[0]:
                best = (cost, head, mid, tail)
    cost, head, mid, tail = best
    head = "".join(TO_LETTER.get(c, c) for c in head)
    mid = "".join(TO_DIGIT.get(c, c) for c in mid)
    tail = "".join(TO_LETTER.get(c, c) for c in tail)
    fixed = head + mid + tail
    if not (PLATE_RE.match(fixed) and cost <= 2.5):
        return fixed, False
    if len(mid) >= 5:  # registration year printed between letters and number
        return f"{head}-{mid[:2]}-{mid[2:]}{tail}", True
    return f"{head}-{mid}{tail}", True


class PlateVoter:
    """Collects readings of one vehicle over many frames and picks the most trusted text."""

    def __init__(self):
        self.scores = defaultdict(float)
        self.count = defaultdict(int)
        self.best_conf = 0.0
        self.det_confs: list[float] = []

    @property
    def det_conf(self) -> float:
        """Average plate-detector confidence: low for things that only look a bit like a plate."""
        return sum(self.det_confs) / len(self.det_confs) if self.det_confs else 0.0

    def add(self, text: str, conf: float, area: float, det_conf: float = 1.0):
        self.det_confs.append(det_conf)
        norm, valid = normalize(text)
        if len(norm) < 3:
            return
        w = conf * (area ** 0.5) * (1.5 if valid else 0.5)
        self.scores[norm] += w
        self.count[norm] += 1
        self.best_conf = max(self.best_conf, conf)

    @property
    def readings(self) -> int:
        return sum(self.count.values())

    def result(self) -> tuple[str | None, float]:
        if not self.scores:
            return None, 0.0
        text = max(self.scores, key=lambda t: self.scores[t])
        share = self.scores[text] / sum(self.scores.values())
        return text, round(min(1.0, self.best_conf * (0.5 + 0.5 * share)), 3)
