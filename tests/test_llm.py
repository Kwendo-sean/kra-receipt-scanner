from app import llm


FACTS = {"claimable input VAT KES": "1,206.90", "receipts": 5, "VAT3 due date": "2026-11-20"}


def test_number_check_accepts_only_fact_numbers():
    ft = llm.facts_to_text(FACTS)
    assert llm.check_numbers("You can claim KES 1,206.90 across 5 receipts by 2026-11-20.", ft) == []
    assert llm.check_numbers("You can claim KES 1206.9.", ft) == []  # same value, other format
    assert llm.check_numbers("You can claim about KES 1,207 across 6 receipts.", ft) == ["1,207", "6"]


def test_write_paragraph_retries_then_falls_back(monkeypatch):
    calls = []

    def fake_chat(system, user, max_tokens=None):
        calls.append(user)
        return "You will save 9,999.00 shillings."  # invented number every time

    monkeypatch.setattr(llm, "chat", fake_chat)
    text, status = llm.write_paragraph("Summarise.", FACTS, "TEMPLATE SENTENCE")
    assert status == "template_rejected" and text == "TEMPLATE SENTENCE"
    assert len(calls) == 2 and "previous answer" in calls[1]


def test_write_paragraph_retry_succeeds(monkeypatch):
    answers = iter(["Claim 1,300.00.", "Claim KES 1,206.90 from 5 receipts."])
    monkeypatch.setattr(llm, "chat", lambda *a, **k: next(answers))
    text, status = llm.write_paragraph("Summarise.", FACTS, "T")
    assert status == "llm_retry" and "1,206.90" in text


def test_llm_down_uses_template(monkeypatch):
    def down(*a, **k):
        raise llm.LLMUnavailable("connection refused")
    monkeypatch.setattr(llm, "chat", down)
    assert llm.write_paragraph("x", FACTS, "T") == ("T", "template_offline")


def test_model_output_is_sanitised(monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "<script>alert('x')</script>**Claim** <b>5</b> receipts")
    text, status = llm.write_paragraph("x", FACTS, "T")
    assert "<" not in text and "*" not in text and status == "llm"


def test_fact_sheet_is_small():
    big = {f"k{i}": "x" * 50 for i in range(100)}
    assert len(llm.facts_to_text(big)) <= llm.FACT_BUDGET_CHARS


def test_chat_caps_max_tokens(monkeypatch):
    sent = {}

    class R:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass

        def post(self, url, json):
            sent.update(json)
            return R()

    monkeypatch.setattr(llm.httpx, "Client", C)
    assert llm.chat("s", "u", max_tokens=5000) == "ok"
    assert sent["max_tokens"] <= 400


def test_think_block_removed(monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "<think>maybe 999 or 1,000</think>Claim KES 1,206.90.")
    text, status = llm.write_paragraph("x", FACTS, "T")
    assert status == "llm" and text == "Claim KES 1,206.90."


def test_model_headings_are_dropped():
    out = llm.sanitize("Main Compliance Problems and Risk Assessment:\nTwo receipts lack eTIMS numbers.\n\n"
                       "**Supplier to Deal With First**\nContact NAIVAS first.")
    assert out == "Two receipts lack eTIMS numbers.\n\nContact NAIVAS first."
    assert llm.sanitize("Claim 5 receipts") == "Claim 5 receipts"  # a lone short answer is kept
