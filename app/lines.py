"""Rebuild text lines from OCR fragments.

RapidOCR returns fragments with boxes, not lines. Labels and amounts on the same
printed line come back as separate fragments, so we regroup them: sort by
vertical centre, merge fragments whose centres lie within half a line-height of
the line, then join each line left to right.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median


@dataclass
class Fragment:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    score: float = 1.0

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> float:
        return max(1.0, self.y1 - self.y0)

    @classmethod
    def from_box(cls, box, text: str, score: float = 1.0) -> "Fragment":
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        return cls(text, min(xs), min(ys), max(xs), max(ys), float(score))


@dataclass
class Line:
    frags: list[Fragment] = field(default_factory=list)

    @property
    def yc(self) -> float:
        return sum(f.yc for f in self.frags) / len(self.frags)

    @property
    def y0(self) -> float:
        return min(f.y0 for f in self.frags)

    @property
    def y1(self) -> float:
        return max(f.y1 for f in self.frags)

    @property
    def h(self) -> float:
        return median(f.h for f in self.frags)

    @property
    def score(self) -> float:
        return min(f.score for f in self.frags)

    @property
    def text(self) -> str:
        fs = sorted(self.frags, key=lambda f: f.x0)
        out = fs[0].text
        for prev, cur in zip(fs, fs[1:]):
            gap = cur.x0 - prev.x1
            out += ("  " if gap > prev.h else " ") + cur.text
        return out.strip()

    def to_dict(self) -> dict:
        return {"text": self.text, "y0": round(self.y0, 1), "y1": round(self.y1, 1), "score": round(self.score, 3)}


def rebuild_lines(frags: list[Fragment]) -> list[Line]:
    frags = [f for f in frags if f.text and f.text.strip()]
    if not frags:
        return []
    med_h = median(f.h for f in frags)
    lines: list[Line] = []
    for f in sorted(frags, key=lambda f: f.yc):
        best, best_d = None, None
        # Only compare with recent lines (fragments arrive in y order).
        for ln in lines[-4:]:
            # compare with the line's horizontally nearest fragment, so a slight
            # residual skew across a wide line doesn't split it
            g = min(ln.frags, key=lambda g: min(abs(f.x0 - g.x1), abs(g.x0 - f.x1)))
            d = min(abs(ln.yc - f.yc), abs(g.yc - f.yc))
            limit = 0.5 * min(max(g.h, f.h), med_h * 1.5)
            if d <= limit and (best_d is None or d < best_d):
                # don't merge two fragments that largely overlap horizontally
                # (they are stacked); OCR boxes often overlap by a few pixels
                if any(min(f.x1, g2.x1) - max(f.x0, g2.x0) > 0.5 * min(f.x1 - f.x0, g2.x1 - g2.x0)
                       for g2 in ln.frags):
                    continue
                best, best_d = ln, d
        if best is None:
            lines.append(Line([f]))
        else:
            best.frags.append(f)
    lines.sort(key=lambda ln: ln.yc)
    return lines


def lines_to_text(lines: list[Line]) -> str:
    return "\n".join(ln.text for ln in lines)
