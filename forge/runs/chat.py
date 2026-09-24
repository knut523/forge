"""Grounded chat — forge's chat answers WITH forge's knowledge, not as a naked LLM.

The plain /api/chat was a passthrough to llm.complete: it knew nothing about the repos
forge has indexed, the PR history it mined, or the memory it holds. "A fortress for the
normal chat" means the chat can reach for the same grounding the reviewer does — so
"why does X work this way?" or "has this been flagged before?" gets a grounded answer.

This retrieves, per the user's last message: matching code (the symbol/keyword index),
past review comments (pr_history), and pinned memory — and injects them as context. It
is best-effort and model-agnostic; if nothing matches, it falls back to a plain answer.
Read-only: it only searches the index, never writes.
"""
from __future__ import annotations

import re

from ..config import llm
from ..indexer import query as Q
from . import pr_history

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]{2,}")
_STOP = {"the", "and", "for", "why", "how", "does", "what", "this", "that", "with",
         "from", "into", "was", "are", "has", "have", "when", "where", "which", "who",
         "you", "can", "should", "would", "there", "here", "about", "forge", "code"}


def _terms(text: str, k: int = 6) -> list[str]:
    seen, out = set(), []
    for w in _WORD.findall(text or ""):
        lw = w.lower()
        if lw in _STOP or lw in seen:
            continue
        seen.add(lw)
        out.append(w)
        if len(out) >= k:
            break
    return out


def _ground(store, repo: str | None, question: str) -> str:
    """A compact grounding block from the index for the user's question."""
    terms = _terms(question)
    if not terms:
        return ""
    blocks: list[str] = []
    # Code matches (symbol/keyword index)
    hits = []
    for t in terms[:4]:
        try:
            hits += Q.search(store, t, repo, limit=4) or []
        except Exception:
            pass
    if hits:
        seen, lines = set(), []
        for h in hits:
            key = (h.get("path"), h.get("name"))
            if key in seen:
                continue
            seen.add(key)
            loc = h.get("path", "")
            nm = h.get("name") or h.get("kind") or ""
            lines.append(f"  {loc}" + (f" · {nm}" if nm else ""))
            if len(lines) >= 10:
                break
        if lines:
            blocks.append("Relevant code in the indexed repos:\n" + "\n".join(lines))
    # Past review comments on this topic (what a human reviewer already said)
    try:
        prs = pr_history.search(" ".join(terms[:4]), repo, limit=4)
    except Exception:
        prs = []
    if prs:
        lines = [f"  PR #{p.get('number')} [{p.get('repo','')}] "
                 f"{(p.get('body') or p.get('title') or '')[:160]}" for p in prs]
        blocks.append("Related past PRs / review comments:\n" + "\n".join(lines))
    if not blocks:
        return ""
    return ("FORGE CONTEXT (retrieved from the index — cite it when relevant, and say when "
            "the answer isn't grounded in it):\n\n" + "\n\n".join(blocks))


def answer(cfg, store, messages: list[dict], model: dict, token: str | None,
           repo: str | None = None) -> dict:
    """Grounded multi-turn answer. Returns {text, model, grounded}."""
    msgs = messages or []
    last_user = next((m.get("content", "") for m in reversed(msgs)
                      if m.get("role") == "user"), "")
    grounding = _ground(store, repo, last_user) if store is not None else ""
    base_system = "\n\n".join(m.get("content", "") for m in msgs
                              if m.get("role") == "system") or \
        ("You are forge's assistant. You have access to retrieved context from the "
         "indexed repositories and past reviews. Answer precisely and cite files/PRs "
         "when you use them; be honest when the context does not cover the question.")
    system = base_system + (("\n\n" + grounding) if grounding else "")
    convo = "\n\n".join(f'{m.get("role", "user").upper()}: {m.get("content", "")}'
                        for m in msgs if m.get("role") != "system")
    text, meta = llm.complete(model, token, system, convo, max_tokens=2000)
    return {"text": text, "model": model.get("name"), "grounded": bool(grounding),
            "error": meta.get("error") if text is None else None}
