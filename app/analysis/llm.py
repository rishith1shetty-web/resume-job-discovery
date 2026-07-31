"""Optional LLM assistance - INTERFACE ONLY, NOT IMPLEMENTED.

CLAUDE.md section 7 permits an LLM to assist extraction, but deterministic
validation stays authoritative. Per the Phase 2 instructions this is defined
behind an interface and left disabled and unimplemented until explicitly
approved.

Guardrails:
  * ``Settings.enable_llm_assist`` defaults to False and its validator refuses
    to start the application if it is turned on.
  * The only concrete implementation is ``NullLLMAssistant``, which returns
    nothing.
  * The deterministic pipeline never calls this module. Removing the file
    would not change any current behaviour.

If this is ever built, the contract is: the assistant may only *supplement*
fields the deterministic extractor left empty, and may never overwrite a
deterministic result or introduce a role outside the taxonomy.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMProfileAssistant(Protocol):
    """Supplementary extraction. Advisory only - never authoritative."""

    async def suggest_missing_fields(
        self, resume_text: str, deterministic: dict[str, Any]
    ) -> dict[str, Any]:
        """Propose values for fields deterministic extraction could not fill.

        Implementations must return only keys that are absent or empty in
        ``deterministic``. Anything else must be discarded by the caller.
        """
        ...


class NullLLMAssistant:
    """The only implementation: does nothing, by design."""

    enabled = False

    async def suggest_missing_fields(
        self, resume_text: str, deterministic: dict[str, Any]
    ) -> dict[str, Any]:
        return {}


def get_llm_assistant() -> NullLLMAssistant:
    """Always returns the null assistant.

    Deterministic extraction must work with no API key configured, so there is
    no branch here on credentials or provider.
    """
    return NullLLMAssistant()
