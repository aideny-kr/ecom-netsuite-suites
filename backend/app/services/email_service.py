"""Email service — console provider for dev, extensible for production."""

import os

import structlog

logger = structlog.get_logger()

EMAIL_PROVIDER = os.environ.get("EMAIL_PROVIDER", "console")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")


async def send_invite_email(
    *,
    to_email: str,
    inviter_name: str,
    tenant_brand_name: str,
    role_display_name: str,
    token: str,
) -> None:
    """Send an invite email. In dev mode, prints to console."""
    accept_url = f"{FRONTEND_URL}/invite/{token}"

    subject = f"{inviter_name} invited you to {tenant_brand_name} on SuiteStudio"
    body = (
        f"{inviter_name} has invited you to join {tenant_brand_name} "
        f"on SuiteStudio as a {role_display_name}.\n\n"
        f"Accept your invitation: {accept_url}\n\n"
        f"This invitation expires in 7 days."
    )

    if EMAIL_PROVIDER == "console":
        print(f"\n{'='*60}", flush=True)
        print("INVITE EMAIL (console mode)", flush=True)
        print(f"To: {to_email}", flush=True)
        print(f"Subject: {subject}", flush=True)
        print(f"Body:\n{body}", flush=True)
        print(f"Accept URL: {accept_url}", flush=True)
        print(f"{'='*60}\n", flush=True)
        return

    logger.info("email.send", provider=EMAIL_PROVIDER, to=to_email, subject=subject)
    raise NotImplementedError(f"Email provider '{EMAIL_PROVIDER}' not yet implemented")
