"""Translate subtitle text line-by-line.

Backends:
  google  free, no key (unofficial Google Translate web endpoint via deep-translator)
  deepl   better Finnish; free tier needs DEEPL_API_KEY (500k chars/month)
  argos   fully offline (Argos Translate); install with `uv sync --extra offline`
"""

from __future__ import annotations

import os
import re
import time

SEP = "\n@@@\n"
TAG_RE = re.compile(r"<[^>]+>|\{\\[^}]*\}")
BATCH_CHARS = 4000


def _clean(text: str) -> str:
    return TAG_RE.sub("", text).strip()


def _make_backend(name: str, target: str):
    if name == "deepl":
        return _DeeplBackend(target)
    if name == "argos":
        return _ArgosBackend(target)
    from deep_translator import GoogleTranslator

    return GoogleTranslator(source="auto", target=target)


class _DeeplBackend:
    """DeepL REST API with header auth (DeepL no longer accepts the key as a URL param)."""

    MAX_TEXTS = 50

    def __init__(self, target: str):
        import requests

        key = (os.environ.get("DEEPL_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("DEEPL_API_KEY is not set")
        host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
        self.url = f"https://{host}/v2/translate"
        self.target = target.upper()
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"DeepL-Auth-Key {key}"

    def translate_many(self, texts: list[str]) -> list[str]:
        out: list[str] = []
        for i in range(0, len(texts), self.MAX_TEXTS):
            chunk = texts[i : i + self.MAX_TEXTS]
            r = self.session.post(
                self.url,
                json={"text": chunk, "source_lang": "EN", "target_lang": self.target},
                timeout=60,
            )
            if r.status_code == 403:
                raise RuntimeError("DeepL rejected the API key (403). Check DEEPL_API_KEY.")
            if r.status_code == 456:
                raise RuntimeError("DeepL monthly character quota used up (456).")
            r.raise_for_status()
            out += [t["text"] for t in r.json()["translations"]]
        return out


class _ArgosBackend:
    def __init__(self, target: str):
        try:
            import argostranslate.package as pkg
            import argostranslate.translate as tr
        except ImportError as e:
            raise RuntimeError("Offline backend not installed. Run: uv sync --extra offline") from e
        installed = {(l.from_code, l.to_code) for l in pkg.get_installed_packages()}
        if ("en", target) not in installed:
            pkg.update_package_index()
            match = [p for p in pkg.get_available_packages() if p.from_code == "en" and p.to_code == target]
            if not match:
                raise RuntimeError(f"No offline en->{target} model available")
            pkg.install_from_path(match[0].download())
        self._tr = tr
        self.target = target

    def translate(self, text: str) -> str:
        return self._tr.translate(text, "en", self.target)


def _translate_chunk(backend, texts: list[str]) -> list[str]:
    joined = SEP.join(texts)
    for attempt in range(3):
        try:
            out = backend.translate(joined) or ""
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    parts = [p.strip() for p in re.split(r"\s*@@@\s*", out)]
    if len(parts) == len(texts):
        return parts
    # Separator got mangled; fall back to one request per line.
    return [backend.translate(t) or "" if t else "" for t in texts]


def translate_lines(
    texts: list[str], target: str = "fi", backend: str = "google", progress=None
) -> list[str]:
    tr = _make_backend(backend, target)
    cleaned = [_clean(t).replace("\n", " ") for t in texts]
    if hasattr(tr, "translate_many"):
        result = []
        for i in range(0, len(cleaned), 200):
            result += tr.translate_many(cleaned[i : i + 200])
            if progress:
                progress(len(result), len(cleaned))
        return result
    result: list[str] = []
    batch: list[str] = []
    size = 0
    for t in cleaned:
        if batch and size + len(t) + len(SEP) > BATCH_CHARS:
            result += _translate_chunk(tr, batch)
            if progress:
                progress(len(result), len(cleaned))
            batch, size = [], 0
        batch.append(t)
        size += len(t) + len(SEP)
    if batch:
        result += _translate_chunk(tr, batch)
        if progress:
            progress(len(result), len(cleaned))
    return result
