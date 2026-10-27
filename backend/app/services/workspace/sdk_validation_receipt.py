"""Internal, context-bound SDK evidence; signing never grants target authority.

No worker caller or durable store is enabled here. A future caller must commit
the exclusive attempt/nonce, recheck current authority, persist this envelope
and verify it with the expected database context before using the evidence.
Keys are trusted application inputs and never enter the credential container.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import asdict

from app.services.workspace.credential_contract import AuthSeederError
from app.services.workspace.suitecloud_sdk import SDK_SHA256, SDK_VERSION, VerifiedIdentity
from app.services.workspace.trusted_sdf_project import TrustedProjectError, build_ready_reviewed_project

_CONTEXT = {"tenant_id", "workspace_id", "changeset_id", "run_id", "actor_id", "connection_id"}
_SHA = re.compile(r"[a-f0-9]{64}", re.ASCII)
SCHEMA = "suite-studio-sdk-validation-v1"


def require_receipt_inputs(*, context: dict, attempt_nonce: str, key: bytes) -> dict:
    try:
        if type(context) is not dict or set(context) != _CONTEXT:
            raise ValueError()
        if any(type(v) is not str or str(uuid.UUID(v)) != v for v in context.values()):
            raise ValueError()
        if type(attempt_nonce) is not str or not _SHA.fullmatch(attempt_nonce):
            raise ValueError()
        if type(key) is not bytes or not 32 <= len(key) <= 4096:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise AuthSeederError("SDK validation requires a bound attempt context and receipt key") from None
    return context.copy()


def require_validation_result(*, result: dict, identity: VerifiedIdentity, inputs: dict) -> dict:
    """Rebuild independently; no child-supplied hash or success flag is trusted."""
    try:
        project = build_ready_reviewed_project(**inputs)
    except (TrustedProjectError, TypeError):
        raise AuthSeederError("SDK validation evidence requires the exact supported project") from None
    expected = {
        "identity": asdict(identity),
        "source_snapshot_sha": inputs["expected_snapshot_sha"],
        "source_manifest_sha": inputs["expected_manifest_sha"],
        "project_format": inputs["project_format"],
        "projected_snapshot_sha": project.snapshot_sha,
        "projected_manifest_sha": project.manifest_sha,
        "project_sha": project.project_sha,
        "sdk_version": SDK_VERSION,
        "sdk_sha256": SDK_SHA256,
        "server_validation": True,
        "account_specific_values": "ERROR",
    }
    if (
        type(result) is not dict
        or set(result) != set(expected) | {"sdk_output_sha256"}
        or type(result.get("sdk_output_sha256")) is not str
        or not _SHA.fullmatch(result["sdk_output_sha256"])
        or result.get("server_validation") is not True
        or any(result.get(k) != v for k, v in expected.items())
    ):
        raise AuthSeederError("SDK validation receipt differs from the exact requested project or identity")
    # Roundtrip disconnects all nested mutable containers from the caller.
    return json.loads(json.dumps(result))


def _mac(payload: dict, key: bytes) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hmac.new(key, SCHEMA.encode("ascii") + b"\0" + body, hashlib.sha256).hexdigest()


def seal_validation_receipt(
    *, result: dict, identity: VerifiedIdentity, inputs: dict, context: dict, attempt_nonce: str, image: str, key: bytes
) -> dict:
    context = require_receipt_inputs(context=context, attempt_nonce=attempt_nonce, key=key)
    if type(image) is not str or not re.fullmatch(r"sha256:[a-f0-9]{64}", image, re.ASCII):
        raise AuthSeederError("SDK validation receipt requires an immutable image")
    payload = {
        "schema": SCHEMA,
        "context": context,
        "attempt_nonce": attempt_nonce,
        "image": image,
        "validation": require_validation_result(result=result, identity=identity, inputs=inputs),
    }
    return {**payload, "mac": _mac(payload, key)}


def verify_validation_receipt(
    *,
    envelope: dict,
    identity: VerifiedIdentity,
    inputs: dict,
    context: dict,
    attempt_nonce: str,
    image: str,
    key: bytes,
) -> dict:
    """Verify serialized evidence against independently loaded expected state.

    A valid MAC is integrity evidence only. It proves neither human approval,
    current permissions, external deployment, health nor single-use consumption.
    """
    require_receipt_inputs(context=context, attempt_nonce=attempt_nonce, key=key)
    try:
        if type(envelope) is not dict or set(envelope) != {
            "schema",
            "context",
            "attempt_nonce",
            "image",
            "validation",
            "mac",
        }:
            raise ValueError()
        expected = seal_validation_receipt(
            result=envelope["validation"],
            identity=identity,
            inputs=inputs,
            context=context,
            attempt_nonce=attempt_nonce,
            image=image,
            key=key,
        )
        if (
            type(envelope["mac"]) is not str
            or not _SHA.fullmatch(envelope["mac"])
            or not hmac.compare_digest(envelope["mac"], expected["mac"])
            or any(envelope[k] != expected[k] for k in expected if k != "mac")
        ):
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise AuthSeederError("SDK validation evidence does not match the expected attempt") from None
    return expected["validation"]
