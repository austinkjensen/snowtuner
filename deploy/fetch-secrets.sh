#!/usr/bin/env bash
#
# Pull Snowflake credentials from AWS Secrets Manager and write them to
# /var/lib/snowtuner/ in a form snowtuner's env_backend can read.
#
# Idempotent: safe to run on every boot via systemd, on demand from a shell,
# or after rotating the secret in Secrets Manager.  Writes two files:
#
#   /var/lib/snowtuner/snowflake_rsa_key.p8   — Snowflake service-user PEM key (mode 0600)
#   /var/lib/snowtuner/env                    — systemd EnvironmentFile= with SNOWTUNER_*
#
# Secret schema (JSON in Secrets Manager):
#
#   {
#     "account":          "abc-12345",
#     "user":             "SNOWTUNER_SVC",
#     "warehouse":        "COMPUTE_WH",
#     "role":             "SNOWTUNER_ROLE",
#     "private_key_pem":  "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n",
#     "api_token":        "<32 url-safe bytes>"      # optional; omit to let snowtuner auto-gen
#   }
#
# Required env (set by systemd unit or interactive caller):
#   SNOWTUNER_SECRET_ID                — the Secrets Manager secret ARN or name
#   AWS_REGION                         — region of the secret
#
# Failure modes that exit non-zero:
#   - aws CLI missing
#   - Secret missing or unreadable (IAM policy gap)
#   - Required JSON field missing
set -euo pipefail

SECRET_ID="${SNOWTUNER_SECRET_ID:-snowtuner/snowflake}"
REGION="${AWS_REGION:-us-west-2}"
DATA_DIR="${SNOWTUNER_DATA_DIR:-/var/lib/snowtuner}"
KEY_PATH="${DATA_DIR}/snowflake_rsa_key.p8"
ENV_PATH="${DATA_DIR}/env"

command -v aws >/dev/null || { echo "aws CLI not installed" >&2; exit 1; }
command -v jq  >/dev/null || { echo "jq not installed"      >&2; exit 1; }

mkdir -p "${DATA_DIR}"

echo "Fetching ${SECRET_ID} from region ${REGION}..." >&2
secret_json=$(aws secretsmanager get-secret-value \
    --secret-id "${SECRET_ID}" \
    --region "${REGION}" \
    --query SecretString \
    --output text)

extract() {
    local field="$1"
    local value
    value=$(jq -r --arg f "$field" '.[$f] // empty' <<< "${secret_json}")
    if [[ -z "${value}" ]]; then
        echo "Secret ${SECRET_ID} is missing required field: ${field}" >&2
        exit 1
    fi
    printf '%s' "${value}"
}

extract_optional() {
    local field="$1"
    jq -r --arg f "$field" '.[$f] // empty' <<< "${secret_json}"
}

# Write the RSA private key.  Use a temp file + rename so a partial write
# during a transient AWS error doesn't leave a corrupt key on disk.
tmp_key="$(mktemp "${KEY_PATH}.XXXXXX")"
extract private_key_pem > "${tmp_key}"
chmod 0600 "${tmp_key}"
mv "${tmp_key}" "${KEY_PATH}"

# Build the systemd EnvironmentFile.  Same write-then-rename for atomicity.
tmp_env="$(mktemp "${ENV_PATH}.XXXXXX")"
{
    printf 'SNOWTUNER_SNOWFLAKE_ACCOUNT=%s\n'        "$(extract account)"
    printf 'SNOWTUNER_SNOWFLAKE_USER=%s\n'           "$(extract user)"
    # NOTE: must match the AuthMethod enum value in src/snowtuner/credentials/model.py
    # — that's "key_pair" with an underscore.  Spelling "keypair" or "key-pair" silently
    # falls back to PASSWORD mode and yields a confusing "password auth requires a
    # password" downstream.  env_backend.py also normalizes both forms as belt-and-suspenders.
    printf 'SNOWTUNER_SNOWFLAKE_AUTHENTICATOR=%s\n'  "key_pair"
    printf 'SNOWTUNER_SNOWFLAKE_PRIVATE_KEY_PATH=%s\n' "${KEY_PATH}"
    wh="$(extract_optional warehouse)"
    [[ -n "${wh}" ]] && printf 'SNOWTUNER_SNOWFLAKE_WAREHOUSE=%s\n' "${wh}"
    rl="$(extract_optional role)"
    [[ -n "${rl}" ]] && printf 'SNOWTUNER_SNOWFLAKE_ROLE=%s\n' "${rl}"
    tk="$(extract_optional api_token)"
    [[ -n "${tk}" ]] && printf 'SNOWTUNER_API_TOKEN=%s\n' "${tk}"
} > "${tmp_env}"
chmod 0640 "${tmp_env}"
mv "${tmp_env}" "${ENV_PATH}"

# Ownership: the systemd service runs as user 'snowtuner'.  Make sure that
# user can read the files we just wrote.
if id snowtuner >/dev/null 2>&1; then
    chown snowtuner:snowtuner "${KEY_PATH}" "${ENV_PATH}"
fi

echo "Wrote ${KEY_PATH} and ${ENV_PATH}" >&2
