"""Email service — console (dev), Resend (production)."""

import os

import httpx
import structlog

logger = structlog.get_logger()

EMAIL_PROVIDER = os.environ.get("EMAIL_PROVIDER", "console")
EMAIL_API_KEY = os.environ.get("EMAIL_API_KEY", "")
EMAIL_FROM_ADDRESS = os.environ.get("EMAIL_FROM_ADDRESS", "SuiteStudio <noreply@suitestudio.ai>")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")


def _build_invite_html(
    inviter_name: str,
    tenant_brand_name: str,
    role_display_name: str,
    accept_url: str,
) -> str:
    return f"""\
<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 480px; margin: 0 auto; padding: 40px 20px;">
  <h2 style="color: #1a1a1a; font-size: 20px; margin-bottom: 8px;">You're invited to join {tenant_brand_name}</h2>
  <p style="color: #666; font-size: 15px; line-height: 1.6; margin-bottom: 24px;">
    {inviter_name} has invited you to join <strong>{tenant_brand_name}</strong> on SuiteStudio as a <strong>{role_display_name}</strong>.
  </p>
  <a href="{accept_url}" style="display: inline-block; background: #1a73e8; color: #fff; text-decoration: none; padding: 12px 28px; border-radius: 8px; font-size: 15px; font-weight: 600;">
    Accept Invitation
  </a>
  <p style="color: #999; font-size: 13px; margin-top: 32px;">
    This invitation expires in 7 days. If you didn't expect this email, you can safely ignore it.
  </p>
</div>
"""


async def send_invite_email(
    *,
    to_email: str,
    inviter_name: str,
    tenant_brand_name: str,
    role_display_name: str,
    token: str,
) -> None:
    """Send an invite email via configured provider."""
    accept_url = f"{FRONTEND_URL}/invite/{token}"

    subject = f"{inviter_name} invited you to {tenant_brand_name} on SuiteStudio"
    text_body = (
        f"{inviter_name} has invited you to join {tenant_brand_name} "
        f"on SuiteStudio as a {role_display_name}.\n\n"
        f"Accept your invitation: {accept_url}\n\n"
        f"This invitation expires in 7 days."
    )
    html_body = _build_invite_html(inviter_name, tenant_brand_name, role_display_name, accept_url)

    if EMAIL_PROVIDER == "console":
        print(f"\n{'='*60}", flush=True)
        print("INVITE EMAIL (console mode)", flush=True)
        print(f"To: {to_email}", flush=True)
        print(f"Subject: {subject}", flush=True)
        print(f"Body:\n{text_body}", flush=True)
        print(f"Accept URL: {accept_url}", flush=True)
        print(f"{'='*60}\n", flush=True)
        return

    if EMAIL_PROVIDER == "resend":
        await _send_via_resend(to_email, subject, html_body, text_body)
        return

    raise NotImplementedError(f"Email provider '{EMAIL_PROVIDER}' not yet implemented")


async def _send_via_resend(to: str, subject: str, html: str, text: str) -> None:
    """Send email via Resend API."""
    if not EMAIL_API_KEY:
        raise ValueError("EMAIL_API_KEY is required for Resend provider")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {EMAIL_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": EMAIL_FROM_ADDRESS,
                "to": [to],
                "subject": subject,
                "html": html,
                "text": text,
            },
            timeout=10.0,
        )

    if response.status_code not in (200, 201):
        logger.error("email.resend_failed", status=response.status_code, body=response.text)
        raise RuntimeError(f"Resend API error: {response.status_code} — {response.text}")

    logger.info("email.sent", provider="resend", to=to, subject=subject)
