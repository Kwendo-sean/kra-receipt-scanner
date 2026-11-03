"""Local language model (llama-server, OpenAI-compatible) for report prose only.

The model never computes a figure. It gets a small fact sheet per section and
writes one or two short paragraphs. Every number in its output must appear in
the fact sheet; otherwise we retry once, then fall back to a template sentence.
"""
from __future__ import annotations

import re
import threading
from decimal import Decimal, InvalidOperation

import bleach
import httpx

from app import config

_lock = threading.Lock()  # one request at a time (llama-server runs one well)
FACT_BUDGET_CHARS = 2000  # ~550 tokens; llama-server runs with a 2048-token context

SYSTEM = (
    "You write clear, plain-English paragraphs for a Kenyan small-business VAT and spending report. "
    "Use ONLY the facts given. Copy every number exactly as written in the facts, with KES before amounts. "
    "Never calculate, add, subtract, round, estimate or invent numbers, dates or names. "
    "Be specific: name the suppliers and amounts from the facts and say what to do. "
    "No headings, no lists, no markdown. Separate paragraphs with a blank line. At most three short paragraphs."
)


class LLMUnavailable(RuntimeError):
    pass


def _client(timeout: float | None = None) -> httpx.Client:
    return httpx.Client(timeout=timeout or config.settings.llm_timeout)


def available(timeout: float = 3.0) -> bool:
    try:
        with _client(timeout) as c:
            r = c.get(config.settings.llm_url.rstrip("/") + "/models")
            return r.status_code == 200
    except httpx.HTTPError:
        return False


def chat(system: str, user: str, max_tokens: int | None = None) -> str:
    s = config.settings
    body = {
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": min(max_tokens or s.llm_max_tokens, s.llm_max_tokens),
        "temperature": 0.2,
        "stream": False,
    }
    if s.llm_model:
        body["model"] = s.llm_model
    with _lock:
        try:
            with _client() as c:
                r = c.post(s.llm_url.rstrip("/") + "/chat/completions", json=body)
        except httpx.HTTPError as e:
            raise LLMUnavailable(str(e)) from e
    if r.status_code != 200:
        raise LLMUnavailable(f"HTTP {r.status_code}")
    try:
        return r.json()["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, ValueError) as e:
        raise LLMUnavailable("Bad response") from e


# ------------------------------------------------------------------ number check

_NUM = re.compile(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?")


def _norm_num(tok: str) -> Decimal | None:
    t = tok.replace(",", "").rstrip(".")
    try:
        return Decimal(t).normalize()
    except InvalidOperation:
        return None


def numbers_in(text: str) -> set[Decimal]:
    out = set()
    for m in _NUM.finditer(text):
        v = _norm_num(m.group(0))
        if v is not None:
            out.add(v)
            # dates like 2026-10-21 contribute 2026, 10, 21 (handled by the regex)
    return out


def check_numbers(output: str, facts_text: str) -> list[str]:
    """Numbers in `output` that do not appear in `facts_text`."""
    allowed = numbers_in(facts_text)
    bad = []
    for m in _NUM.finditer(output):
        v = _norm_num(m.group(0))
        if v is not None and v not in allowed:
            bad.append(m.group(0))
    return bad


def facts_to_text(facts: dict) -> str:
    lines = []
    for k, v in facts.items():
        if isinstance(v, (list, tuple)):
            v = "; ".join(str(x) for x in v)
        lines.append(f"{k}: {v}")
    text = "\n".join(lines)
    return text[:FACT_BUDGET_CHARS]


def sanitize(text: str) -> str:
    """Plain text only: strip any markup the model produced. (Templates also autoescape.)"""
    # reasoning models (e.g. Qwen3) may emit a <think> block first: drop it whole
    text = re.sub(r"<think>.*?(</think>|$)", "", text, flags=re.S | re.I)
    t = bleach.clean(text, tags=[], attributes={}, strip=True)
    t = re.sub(r"[*_#`>]+", "", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def write_paragraph(task: str, facts: dict, fallback: str, enabled: bool = True) -> tuple[str, str]:
    """Return (text, status). status: 'llm' | 'llm_retry' | 'template_rejected' | 'template_offline'."""
    if not enabled:
        return fallback, "template_offline"
    facts_text = facts_to_text(facts)
    user = f"Facts:\n{facts_text}\n\nTask: {task}"
    for attempt in (1, 2):
        try:
            out = sanitize(chat(SYSTEM, user))
        except LLMUnavailable:
            return fallback, "template_offline"
        if out and not check_numbers(out, facts_text) and len(out) < 2400:
            return out, "llm" if attempt == 1 else "llm_retry"
        user = (f"Facts:\n{facts_text}\n\nTask: {task}\n"
                "Your previous answer used numbers that are not in the facts. "
                "Use only numbers copied exactly from the facts.")
    return fallback, "template_rejected"
