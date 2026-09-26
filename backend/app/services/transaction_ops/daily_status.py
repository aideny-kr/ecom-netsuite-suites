"""Read-only daily scan health, distinct from financial certification."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, and_, cast, func, or_, select

from app.models.transaction_ops import TransactionRun
from app.schemas.transaction_runs import ReviewSpan
from app.services.transaction_ops import state_service
from app.services.transaction_ops.daily_evidence import coverage_receipt, reuses_coverage
from app.services.transaction_ops.periods import ReconciliationPolicy, scheduled_window


async def daily_status(db, tenant_id, *, now=None):
    now = now or datetime.now(timezone.utc)
    configs = await state_service.list_configs(db, tenant_id)
    result = []
    for config in configs:
        policy = ReconciliationPolicy.model_validate((config.mapping_json or {}).get("reconciliation_policy") or {})
        _, expected = scheduled_window(policy, now)
        r = TransactionRun
        # Old mappings cannot establish coverage under the current policy.
        candidates = await db.scalars(
            select(r)
            .where(
                r.tenant_id == tenant_id,
                r.config_id == config.id,
                r.origin == "schedule",
                r.status == "finished",
                r.termination_reason == "done",
                r.config_snapshot["mapping_json"] == config.mapping_json,
                func.coalesce(r.params_json["window_basis"].astext, "updated_at") == "updated_at",
                *[
                    r.config_snapshot[key].astext
                    == (str(getattr(config, key)) if getattr(config, key) is not None else None)
                    for key in (
                        "source_connection_id",
                        "source_step_id",
                        "netsuite_connection_id",
                        "netsuite_account_id",
                        "subsidiary_id",
                        "record_type",
                    )
                ],
                r.progress_json["scan_complete"].astext == "true",
                r.progress_json["refund_scan_complete"].astext == "true",
                r.progress_json["destination_scan_complete"].astext == "true",
                or_(
                    r.progress_json["reused_observation_run_ids"].astext.is_not(None),
                    and_(
                        r.progress_json["dependency_scan_complete"].astext == "true",
                        r.progress_json["dependency_index_seed"]["complete"].astext == "true",
                    )
                    if (config.mapping_json or {}).get("metabase_replica")
                    and (config.mapping_json or {}).get("reconciliation_policy") is not None
                    else True,
                ),
            )
            .order_by(cast(r.params_json["window_end"].astext, DateTime(timezone=True)).desc(), r.created_at.desc())
            .limit(32)
        )
        latest, reused = None, None
        for candidate in candidates:
            proof = None
            if reuses_coverage(candidate):
                # A receipt is not a new scan. Revalidate its original sources,
                # including today's contract, rather than trusting copied flags.
                current = state_service._config_snapshot(config)
                if any(
                    candidate.config_snapshot.get(key, 1) != current[key]
                    for key in ("evidence_contract_version", "destination_discovery_version")
                ):
                    continue
                try:
                    span = ReviewSpan(
                        id=candidate.id,
                        start=candidate.params_json["window_start"],
                        end=candidate.params_json["window_end"],
                    )
                    proof = await coverage_receipt(
                        db,
                        candidate,
                        span,
                        source_ids=candidate.progress_json.get("reused_observation_run_ids", []),
                        now=now,
                    )
                except (TypeError, ValueError, KeyError):
                    continue
                if proof is None:
                    continue
            latest, reused = candidate, proof
            break
        end = datetime.fromisoformat(latest.params_json["window_end"]) if latest else None
        zone = ZoneInfo(policy.timezone_name)
        active = config.enabled and config.schedule_enabled
        result.append(
            {
                "config_id": str(config.id),
                "enabled": active,
                "status": "paused"
                if not active
                else "up_to_date"
                if end and end >= expected
                else "behind"
                if end
                else "not_verified",
                "completed_until": end.isoformat() if end else None,
                "checked_through": (end.astimezone(zone) - timedelta(microseconds=1)).date().isoformat()
                if end
                else None,
                "expected_until": expected.isoformat(),
                "last_completed_at": reused["reused_scan_completed_at"]
                if reused
                else (latest.finished_at.isoformat() if latest and latest.finished_at else None),
                "run_id": str(latest.id) if latest else None,
                "timezone": policy.timezone_name,
                "completion_basis": "reused_scan_coverage" if reused else "scan_coverage",
                "source_run_ids": reused["reused_observation_run_ids"] if reused else [],
            }
        )
    return result
