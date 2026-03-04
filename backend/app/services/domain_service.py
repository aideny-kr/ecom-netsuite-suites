"""Custom domain verification service."""

from __future__ import annotations

import asyncio
import uuid

import structlog

logger = structlog.get_logger()


def get_verification_record(tenant_id: uuid.UUID) -> dict:
    """Return the DNS TXT record the tenant should create for domain verification."""
    return {
        "type": "TXT",
        "name": "_netsuite-verify",
        "value": f"tenant_{tenant_id}",
    }


async def verify_domain(domain: str, tenant_id: uuid.UUID) -> bool:
    """Verify that a domain has the correct TXT record for the tenant.

    Looks up _netsuite-verify.{domain} TXT records and checks for tenant_{tenant_id}.
    """
    import dns.resolver

    record_name = f"_netsuite-verify.{domain}"
    expected_value = f"tenant_{tenant_id}"

    try:
        # Run DNS query in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        answers = await loop.run_in_executor(
            None, lambda: dns.resolver.resolve(record_name, "TXT")
        )
        for rdata in answers:
            for txt_string in rdata.strings:
                decoded = txt_string.decode("utf-8").strip().strip('"')
                if decoded == expected_value:
                    logger.info(
                        "domain_verified",
                        domain=domain,
                        tenant_id=str(tenant_id),
                    )
                    return True

        logger.warning(
            "domain_verification_failed",
            domain=domain,
            tenant_id=str(tenant_id),
            reason="TXT record found but value mismatch",
        )
        return False

    except Exception as e:
        logger.warning(
            "domain_verification_failed",
            domain=domain,
            tenant_id=str(tenant_id),
            reason=str(e),
        )
        return False
