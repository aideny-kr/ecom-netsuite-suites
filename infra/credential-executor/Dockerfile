# Build locally, review and configure the resulting immutable image ID.
# Operator supplies a digest-pinned Python 3.11 Debian base; never a tenant input.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends default-jre-headless \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir cryptography==46.0.3
COPY backend/app/services/workspace/credential_contract.py /opt/suitestudio/app/services/workspace/credential_contract.py
COPY backend/app/services/workspace/suitecloud_sdk.py /opt/suitestudio/app/services/workspace/suitecloud_sdk.py
# Stage only the public Oracle SDK artifact here; never a credential store/key.
COPY infra/credential-executor/cli-2026.1.0.jar /opt/sdk/cli-2026.1.0.jar
RUN python -c "import hashlib; assert hashlib.sha256(open('/opt/sdk/cli-2026.1.0.jar','rb').read()).hexdigest() == '158038cfdc552e1af4b29aea86ff59ba8c0658ec950129339c1fbace4b1ad78b'"
USER 65532:65532
