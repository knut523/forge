"""Minimal completion client for registered models.

Only what a run needs: one prompt in, text out, with the provider differences
absorbed here. Errors are returned, never raised — a phase should record that
the model failed and carry on degraded, not crash the run.
"""
from __future__ import annotations

import time

import httpx

TIMEOUT = 180.0


def complete(model: dict, token: str | None, system: str, user: str,
             max_tokens: int = 2000) -> tuple[str | None, dict]:
    """(text, meta) with a few retries on transient failures — the shared Claude
    bridge can be briefly overloaded when many runs hit it at once, and a plain
    failure there would sink a whole build for no real reason."""
    last = {}
    for attempt in range(3):
        text, meta = _complete_once(model, token, system, user, max_tokens)
        if text is not None and text.strip():
            return text, meta
        # an empty answer is not a usable result — treat it like a transient failure
        last = meta or {"error": "empty model response"}
        err = str(meta.get("error", "")).lower()
        transient = any(k in err for k in (
            "timeout", "timed out", "connect", "429", "500", "502", "503",
            "504", "reset", "overloaded", "temporarily"))
        if not transient:
            break
        time.sleep(2 * (attempt + 1))
    return None, last


def _complete_once(model: dict, token: str | None, system: str, user: str,
                   max_tokens: int = 2000) -> tuple[str | None, dict]:
    """(text, meta). text is None on failure; meta always explains what happened."""
    provider = (model.get("provider") or "").lower()
    mid = model["model_id"]
    base = (model.get("base_url") or "").rstrip("/")

    try:
        if provider == "claude-cli":
            # The Claude Code session already running on this host, reached
            # through its bridge. No API key of our own — it spends the
            # session's own quota, and the bridge reports no token counts.
            # The bridge may pass the prompt as a single argv element, which the
            # kernel caps at MAX_ARG_STRLEN (~128 KB) — a larger prompt fails with
            # "Argument list too long". Keep the bridge prompt under that here; the
            # HTTP-API providers below have no such limit and get the full context.
            u = user
            if len(u) > 290000:
                u = (u[:290000] + "\n\n… context truncated to fit the session bridge; "
                     "review the rest directly …")
            with httpx.Client(timeout=420.0) as c:
                r = c.post(base or "http://ide:8930/",
                           headers={"Authorization": f"Bearer {token or ''}"},
                           json={"prompt": u, "system": system,
                                 **({"model": mid} if mid and mid != "session" else {})})
            if r.status_code != 200:
                detail = r.json().get("error", r.text[:200]) if r.headers.get(
                    "content-type", "").startswith("application/json") else r.text[:200]
                return None, {"error": f"claude bridge {r.status_code}: {detail}"}
            return r.json().get("text", ""), {"model": mid or "session",
                                              "provider": provider}

        if provider == "anthropic":
            with httpx.Client(timeout=TIMEOUT) as c:
                r = c.post((base or "https://api.anthropic.com") + "/v1/messages",
                           headers={"x-api-key": token or "",
                                    "anthropic-version": "2023-06-01",
                                    "content-type": "application/json"},
                           json={"model": mid, "max_tokens": max_tokens,
                                 "system": system,
                                 "messages": [{"role": "user", "content": user}]})
            if r.status_code != 200:
                return None, {"error": f"anthropic {r.status_code}: {r.text[:200]}"}
            body = r.json()
            text = "".join(b.get("text", "") for b in body.get("content", [])
                           if b.get("type") == "text")
            u = body.get("usage", {})
            return text, {"model": mid, "provider": provider,
                          "input_tokens": u.get("input_tokens"),
                          "output_tokens": u.get("output_tokens")}

        if provider == "ollama":
            with httpx.Client(timeout=TIMEOUT) as c:
                r = c.post((base or "http://127.0.0.1:11434") + "/api/chat",
                           json={"model": mid, "stream": False,
                                 "messages": [{"role": "system", "content": system},
                                              {"role": "user", "content": user}]})
            if r.status_code != 200:
                return None, {"error": f"ollama {r.status_code}: {r.text[:200]}"}
            return r.json().get("message", {}).get("content", ""), {
                "model": mid, "provider": provider}

        # openai and anything speaking its chat-completions dialect
        url = (base or "https://api.openai.com/v1") + "/chat/completions"
        with httpx.Client(timeout=TIMEOUT) as c:
            r = c.post(url, headers={"Authorization": f"Bearer {token or ''}"},
                       json={"model": mid, "max_tokens": max_tokens,
                             "messages": [{"role": "system", "content": system},
                                          {"role": "user", "content": user}]})
        if r.status_code != 200:
            return None, {"error": f"{provider or 'openai'} {r.status_code}: {r.text[:200]}"}
        body = r.json()
        text = body["choices"][0]["message"]["content"]
        u = body.get("usage", {})
        return text, {"model": mid, "provider": provider,
                      "input_tokens": u.get("prompt_tokens"),
                      "output_tokens": u.get("completion_tokens")}
    except Exception as e:
        return None, {"error": f"{type(e).__name__}: {str(e)[:200]}"}
