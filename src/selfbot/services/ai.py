"""AI provider service.

Phase 1 of the AI rework introduces database-backed providers while preserving
backward compatibility: on first start, any keys still present in the
environment are seeded into the ``ai_providers`` table so the bot behaves
exactly as before. The higher-level :class:`AIManager` (completion routing,
cooldown and conversation memory) is layered on top in a later phase.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import AIConfig

__all__ = ["seed_providers_from_env"]

logger = logging.getLogger(__name__)


async def seed_providers_from_env(db: Any, ai: AIConfig) -> int:
    """Copy legacy environment keys into the ``ai_providers`` table.

    Runs once, only when the table is empty. Existing operator configuration
    in the database always wins — we never overwrite a row that is already
    there. Returns the number of providers seeded.
    """
    existing = await db.list_providers()
    if existing:
        return 0

    seeded = 0

    if ai.anyapi_key:
        await db.add_provider(
            "anyapi",
            ai.anyapi_base_url,
            ai.anyapi_key,
            model=ai.anyapi_model,
            kind="openai",
            is_default=True,
        )
        seeded += 1

    if ai.bluesminds_key:
        await db.add_provider(
            "bluesminds",
            ai.bluesminds_base_url,
            ai.bluesminds_key,
            model=ai.bluesminds_model,
            kind="openai",
            is_default=seeded == 0,
        )
        seeded += 1

    if ai.rapidapi_key:
        # The RapidAPI integration uses a non-OpenAI request shape; mark it
        # so the AIManager can route it correctly in phase 2.
        await db.add_provider(
            "rapidapi",
            "https://chatgpt-api8.p.rapidapi.com",
            ai.rapidapi_key,
            model="GPT_5_4_high",
            kind="rapidapi",
            is_default=seeded == 0,
        )
        seeded += 1

    if seeded:
        logger.info("Seeded %d AI provider(s) from environment", seeded)
    return seeded
