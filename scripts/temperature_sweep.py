"""Empirical temperature sweep for M4 agent structured outputs."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from app.agents.contracts import ChangeImpact
from app.agents.mcp_tools import context_json, mcp_change_analyst_context
from app.agents.prompts import PROMPT_VERSION
from app.agents.prompts import v2 as prompts
from app.core.settings import get_settings
from app.graph.state import AnalysisRequest
from app.mcp_client import SentinelFlowMcpAdapter
from app.model_gateway.errors import (
    is_daily_quota_exhausted,
    is_quota_or_rate_limit_error,
    is_transient_api_error,
)
from app.model_gateway.llm import (
    GeminiStructuredLLM,
    OpenRouterStructuredLLM,
    StructuredLLM,
    StructuredOutputError,
    is_openrouter_model,
)
from app.model_gateway.pacing import (
    AsyncMinIntervalPacer,
    estimate_paced_wall_seconds,
    min_interval_for_rpm,
)
from mcp_server.sentinelflow_context.server import mcp
from pydantic import ValidationError

# Gemini Developer API free tier: gemini-3.8-flash often capped at 5 RPM.
_DEFAULT_GEMINI_GENERATION_RPM = 5
_TEMPERATURES = (0.0, 0.2, 1.0)

logger = logging.getLogger(__name__)


class AttemptOutcome(StrEnum):
    OK = "ok"
    INVALID = "invalid_schema"
    RATE_LIMIT = "rate_limit"
    DAILY_QUOTA = "daily_quota_exhausted"
    TRANSIENT_API = "transient_api"
    SKIPPED = "skipped_quota_exhausted"
    OTHER_ERROR = "error"


@dataclass
class SweepArm:
    temperature: float
    attempts: int = 0
    schema_valid: int = 0
    outcomes: list[str] = field(default_factory=list)


@dataclass
class AttemptResult:
    outcome: AttemptOutcome
    detail: str = ""


async def _fetch_analyst_context(
    *,
    change_id: str,
    domain_pack_id: str,
) -> dict[str, object]:
    async with SentinelFlowMcpAdapter(mcp) as adapter:
        context = await mcp_change_analyst_context(
            adapter,
            change_id=change_id,
            domain_pack_id=domain_pack_id,
        )
    return {"deterministic_signals": context}


async def _one_attempt(
    *,
    temperature: float,
    user_prompt: str,
    llm: StructuredLLM,
    model: str,
    pacer: AsyncMinIntervalPacer,
    quota_retry_wait_seconds: float,
) -> AttemptResult:
    await pacer.wait_turn()
    try:
        impact = (
            await llm.generate(
                model=model,
                temperature=temperature,
                system_prompt=prompts.CHANGE_ANALYST_SYSTEM,
                user_prompt=user_prompt,
                schema=ChangeImpact,
            )
        ).value
        ChangeImpact.model_validate(impact.model_dump())
    except (ValidationError, StructuredOutputError) as exc:
        return AttemptResult(AttemptOutcome.INVALID, str(exc)[:200])
    except Exception as exc:  # noqa: BLE001 — sweep must never crash the run
        if is_daily_quota_exhausted(exc):
            return AttemptResult(AttemptOutcome.DAILY_QUOTA, str(exc)[:200])
        if is_quota_or_rate_limit_error(exc):
            logger.warning(
                "RPM/throttle at temp=%s; one retry after %.0fs",
                temperature,
                quota_retry_wait_seconds,
            )
            await asyncio.sleep(quota_retry_wait_seconds)
            try:
                await pacer.wait_turn()
                impact = (
                    await llm.generate(
                        model=model,
                        temperature=temperature,
                        system_prompt=prompts.CHANGE_ANALYST_SYSTEM,
                        user_prompt=user_prompt,
                        schema=ChangeImpact,
                    )
                ).value
                ChangeImpact.model_validate(impact.model_dump())
                return AttemptResult(AttemptOutcome.OK)
            except Exception as retry_exc:  # noqa: BLE001
                if is_daily_quota_exhausted(retry_exc):
                    return AttemptResult(AttemptOutcome.DAILY_QUOTA, str(retry_exc)[:200])
                if is_quota_or_rate_limit_error(retry_exc):
                    return AttemptResult(AttemptOutcome.RATE_LIMIT, str(retry_exc)[:200])
                if is_transient_api_error(retry_exc):
                    return AttemptResult(AttemptOutcome.TRANSIENT_API, str(retry_exc)[:200])
                return AttemptResult(AttemptOutcome.OTHER_ERROR, str(retry_exc)[:200])
        if is_transient_api_error(exc):
            logger.warning("Transient API at temp=%s; one retry after 15s", temperature)
            await asyncio.sleep(15.0)
            try:
                await pacer.wait_turn()
                impact = (
                    await llm.generate(
                        model=model,
                        temperature=temperature,
                        system_prompt=prompts.CHANGE_ANALYST_SYSTEM,
                        user_prompt=user_prompt,
                        schema=ChangeImpact,
                    )
                ).value
                ChangeImpact.model_validate(impact.model_dump())
                return AttemptResult(AttemptOutcome.OK)
            except Exception as retry_exc:  # noqa: BLE001
                if is_transient_api_error(retry_exc):
                    return AttemptResult(AttemptOutcome.TRANSIENT_API, str(retry_exc)[:200])
                return AttemptResult(AttemptOutcome.OTHER_ERROR, str(retry_exc)[:200])
        return AttemptResult(AttemptOutcome.OTHER_ERROR, str(exc)[:200])
    return AttemptResult(AttemptOutcome.OK)


async def run_sweep(
    *,
    change_id: str,
    domain_pack_id: str,
    repeats: int,
    rpm: int,
    model: str,
) -> tuple[dict[float, SweepArm], dict[str, object]]:
    settings = get_settings()
    interval = min_interval_for_rpm(rpm)
    pacer = AsyncMinIntervalPacer(interval)
    llm: StructuredLLM
    if is_openrouter_model(model):
        llm = OpenRouterStructuredLLM(settings=settings, client_max_retries=0)
    else:
        llm = GeminiStructuredLLM(settings=settings, client_max_retries=0)

    request = AnalysisRequest(change_id=change_id, domain_pack_id=domain_pack_id)
    context_payload = await _fetch_analyst_context(
        change_id=change_id,
        domain_pack_id=domain_pack_id,
    )
    user_prompt = (
        f"{context_json(context_payload)}\n"
        f"change_id={request.change_id}\n"
        f"prompt_version={PROMPT_VERSION}"
    )

    arms = {temp: SweepArm(temp) for temp in _TEMPERATURES}
    started = time.monotonic()

    daily_exhausted = False
    for temp in _TEMPERATURES:
        arm = arms[temp]
        for repeat_index in range(repeats):
            if daily_exhausted:
                arm.attempts += 1
                arm.outcomes.append(AttemptOutcome.SKIPPED.value)
                continue
            arm.attempts += 1
            result = await _one_attempt(
                temperature=temp,
                user_prompt=user_prompt,
                llm=llm,
                model=model,
                pacer=pacer,
                quota_retry_wait_seconds=65.0,
            )
            arm.outcomes.append(result.outcome.value)
            if result.outcome is AttemptOutcome.OK:
                arm.schema_valid += 1
            elif result.outcome is AttemptOutcome.DAILY_QUOTA:
                daily_exhausted = True
                logger.error(
                    "Daily free-tier quota exhausted for %s; skipping remaining attempts.",
                    model,
                )
            elif result.detail:
                logger.info(
                    "temp=%s repeat=%s outcome=%s detail=%s",
                    temp,
                    repeat_index + 1,
                    result.outcome.value,
                    result.detail,
                )

    elapsed = time.monotonic() - started
    meta = {
        "change_id": change_id,
        "domain_pack_id": domain_pack_id,
        "model": model,
        "repeats_per_temperature": repeats,
        "temperatures": list(_TEMPERATURES),
        "generation_rpm_limit": rpm,
        "min_interval_seconds": interval,
        "elapsed_seconds": round(elapsed, 1),
        "prompt_version": PROMPT_VERSION,
        "client_max_retries": 0,
        "notes": (
            "Free tier: gemini-3.8-flash is often 5 RPM and 20 RPD; "
            "use --repeats 1 (3 calls) when near daily cap. "
            "SDK retries disabled so paced calls are not multiplied."
        ),
    }
    return arms, meta


def _choose_temperature(arms: dict[float, SweepArm]) -> float | None:
    """Pick highest schema-valid rate; tie-break toward lower temperature."""
    if not arms:
        return None
    ranked = sorted(
        arms.items(),
        key=lambda item: (-item[1].schema_valid / max(item[1].attempts, 1), item[0]),
    )
    best_rate = ranked[0][1].schema_valid / max(ranked[0][1].attempts, 1)
    if best_rate <= 0:
        return None
    return ranked[0][0]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Temperature sweep with Gemini free-tier pacing (Change Analyst schema).",
    )
    parser.add_argument("--change-id", default="CHG-H001")
    parser.add_argument("--domain-pack-id", default="healthcare-claims")
    parser.add_argument(
        "--repeats",
        type=int,
        default=2,
        help="Runs per temperature (default 2 → 6 paced calls; 3 → 9 calls).",
    )
    parser.add_argument(
        "--rpm",
        type=int,
        default=_DEFAULT_GEMINI_GENERATION_RPM,
        help="Max generation requests per minute (free tier default: 5).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override agent model (default: Settings.agent_llm_model).",
    )
    parser.add_argument("--out", type=Path, default=Path("evals/graph/temperature_sweep.json"))
    args = parser.parse_args()

    settings = get_settings()
    model = args.model or settings.agent_llm_model
    call_count = len(_TEMPERATURES) * args.repeats
    interval = min_interval_for_rpm(args.rpm)
    estimate = estimate_paced_wall_seconds(
        call_count=call_count,
        min_interval_seconds=interval,
    )
    print(
        f"Pacing: {args.rpm} RPM → {interval:.1f}s between call starts; "
        f"{call_count} calls ≈ {estimate / 60:.1f} min wall-clock (plus API latency).",
        file=sys.stderr,
    )

    try:
        arms, meta = asyncio.run(
            run_sweep(
                change_id=args.change_id,
                domain_pack_id=args.domain_pack_id,
                repeats=args.repeats,
                rpm=args.rpm,
                model=model,
            ),
        )
    except RuntimeError as exc:
        if "GEMINI_API_KEY" in str(exc) or "OPENROUTER_API_KEY" in str(exc):
            print(f"{exc}; sweep not run.", file=sys.stderr)
            sys.exit(2)
        raise

    chosen = _choose_temperature(arms)
    payload: dict[str, object] = {
        "meta": meta,
        "recommended_agent_llm_temperature": chosen,
        "arms": {
            str(temp): {
                "schema_valid_rate": arm.schema_valid / max(arm.attempts, 1),
                "schema_valid_count": arm.schema_valid,
                "attempts": arm.attempts,
                "outcomes": arm.outcomes,
            }
            for temp, arm in arms.items()
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
