#!/usr/bin/env bash
# ============================================================================
# Deterministic Workload Provisioner (Post-Approval GitHub Actions Executor)
# Enforces mandatory BUDGET_BRL, TTL_MINUTES, ALERT_EMAILS_CSV, and Dual-Pinning
# Anti-Tampering Lock (GIT_TAG == GCP_DEPLOYED_VERSION && SHA == EXPECTED_COMMIT_SHA),
# reconciles Singleton Shared GCLB + DB/Redis (auto-resuming a shared DB server
# stopped by the FinOps idle reaper before Terraform), builds API/BFF container(s) and
# Static SPA bundle (zero container for frontend!), applies Terraform, syncs
# static SPA assets to GCS BackendBucket, registers .nip.io hostRules on GCLB,
# waits until the managed SSL certificate and the HTTPS routes answer with trusted
# TLS on the 3 .nip.io hosts, and only then starts the precision TTL clock for the
# workload-only teardown (the TTL never runs while HTTPS is still provisioning).
# ============================================================================
set -euo pipefail

: "${WORKLOAD_SLUG:?ERRO FATAL: WORKLOAD_SLUG obrigatorio}"
: "${GITHUB_ORG:?ERRO FATAL: GITHUB_ORG obrigatorio}"
: "${SOLUTION_REPO:?ERRO FATAL: SOLUTION_REPO obrigatorio}"
: "${GIT_TAG:?ERRO FATAL: GIT_TAG (GCP_DEPLOYED_VERSION) obrigatorio}"
: "${EXPECTED_COMMIT_SHA:?ERRO FATAL: EXPECTED_COMMIT_SHA obrigatorio}"
: "${TARGET_GCP_PROJECT:?ERRO FATAL: TARGET_GCP_PROJECT obrigatorio}"
: "${BUDGET_BRL:?ERRO FATAL: BUDGET_BRL obrigatorio}"
: "${TTL_LABEL:?ERRO FATAL: TTL_LABEL obrigatorio}"
: "${TTL_MINUTES:?ERRO FATAL: TTL_MINUTES obrigatorio}"
: "${DATABASE_TYPE:?ERRO FATAL: DATABASE_TYPE obrigatorio}"
: "${ENABLE_REDIS:?ERRO FATAL: ENABLE_REDIS obrigatorio}"
: "${ALERT_EMAILS_CSV:?ERRO FATAL: ALERT_EMAILS_CSV obrigatorio}"
: "${SLO_AVAILABILITY_GOAL:?ERRO FATAL: SLO_AVAILABILITY_GOAL obrigatorio}"
: "${SLO_LATENCY_P95_MS:?ERRO FATAL: SLO_LATENCY_P95_MS obrigatorio}"

if [[ "${TARGET_GCP_PROJECT}" != "ernani-demo-argolis" ]]; then
  echo "ERRO DE GOVERNANCA: Projeto alvo '${TARGET_GCP_PROJECT}' nao autorizado (permitido: ernani-demo-argolis)." >&2
  exit 1
fi

if [[ "${GIT_TAG}" != "GCP_DEPLOYED_VERSION" ]]; then
  echo "ERRO DE GOVERNANCA: GIT_TAG deve ser estritamente 'GCP_DEPLOYED_VERSION' (recebido: '${GIT_TAG}')." >&2
  exit 1
fi

REGION="southamerica-east1"
TF_BUCKET="ernani-demo-argolis-sdlc-tfstate"
AR_REPO="${REGION}-docker.pkg.dev/${TARGET_GCP_PROJECT}/sdlc-sandbox-workloads"
SHORT_SHA="${EXPECTED_COMMIT_SHA:0:8}"
APPROVALS_ROOT="$(pwd)"
# Tempo máximo de espera pela validação do certificado SSL gerenciado + rotas HTTPS.
SSL_WAIT_MAX_MINUTES="${SSL_WAIT_MAX_MINUTES:-30}"

echo "=== [1/8] Fetching ephemeral GitHub token & Verifying Cryptographic HMAC-SHA256 Manifest Seal ==="
GH_PAT="$(gcloud secrets versions access latest --secret=sdlc-github-org-pat --project="${TARGET_GCP_PROJECT}")"
echo "::add-mask::${GH_PAT}"
AUTH_B64="$(printf 'x-access-token:%s' "${GH_PAT}" | base64 -w 0)"
export MANIFEST_HMAC_SECRET="${GH_PAT}"
if [[ -f "${APPROVALS_ROOT}/requests/${WORKLOAD_SLUG}/approval-manifest.json" ]]; then
  python3 "${APPROVALS_ROOT}/scripts/enforce_provisioning_policy.py" \
    --mode pre-deploy \
    --manifest "${APPROVALS_ROOT}/requests/${WORKLOAD_SLUG}/approval-manifest.json"
fi

SRC_DIR="$(mktemp -d /tmp/solution-src-XXXXXX)"
SPA_EXTRACT_DIR="$(mktemp -d /tmp/spa-extract-XXXXXX)"
trap 'rm -rfv "${SRC_DIR}" "${SPA_EXTRACT_DIR}" >/dev/null 2>&1 || true' EXIT

echo "=== [2/8] Cloning ${GITHUB_ORG}/${SOLUTION_REPO} & Verifying Anti-Tampering Lock on tag ${GIT_TAG} ==="
GIT_CONFIG_COUNT=1 \
GIT_CONFIG_KEY_0="http.https://github.com/.extraheader" \
GIT_CONFIG_VALUE_0="AUTHORIZATION: basic ${AUTH_B64}" \
GIT_TERMINAL_PROMPT=0 \
git clone "https://github.com/${GITHUB_ORG}/${SOLUTION_REPO}.git" "${SRC_DIR}"

GIT_CONFIG_COUNT=1 \
GIT_CONFIG_KEY_0="http.https://github.com/.extraheader" \
GIT_CONFIG_VALUE_0="AUTHORIZATION: basic ${AUTH_B64}" \
GIT_TERMINAL_PROMPT=0 \
git -C "${SRC_DIR}" fetch --tags --force origin

TAG_COMMIT_SHA="$(git -C "${SRC_DIR}" rev-parse "refs/tags/${GIT_TAG}^{commit}")"
if [[ "${TAG_COMMIT_SHA}" != "${EXPECTED_COMMIT_SHA}"* && "${EXPECTED_COMMIT_SHA}" != "${TAG_COMMIT_SHA}"* ]]; then
  echo "ERRO FATAL DE SEGURANCA (ANTI-TAMPERING / TOCTOU): A tag '${GIT_TAG}' resolve para '${TAG_COMMIT_SHA}', divergindo do commit auditado e aprovado no PR ('${EXPECTED_COMMIT_SHA}')! Deploy abortado!" >&2
  exit 1
fi

git -C "${SRC_DIR}" checkout "refs/tags/${GIT_TAG}"
COUNTS="$(git -C "${SRC_DIR}" rev-list --left-right --count "origin/main...refs/tags/${GIT_TAG}" || echo "0	0")"
BEHIND_MAIN="$(echo "${COUNTS}" | awk '{print $1}')"
AHEAD_MAIN="$(echo "${COUNTS}" | awk '{print $2}')"
COMPARE_URL="https://github.com/${GITHUB_ORG}/${SOLUTION_REPO}/compare/main...${GIT_TAG}"
echo "Tag verificada com sucesso: ${GIT_TAG} -> ${TAG_COMMIT_SHA} (${AHEAD_MAIN} ahead, ${BEHIND_MAIN} behind main | ${COMPARE_URL})"

if [[ ! -d "${SRC_DIR}/infra/terraform" ]]; then
  echo "ERRO: A tag ${GIT_TAG} em ${SOLUTION_REPO} nao contem infra/terraform preparado para o deploy." >&2
  exit 1
fi

echo "=== [2.5/8] Enforcing Mandatory OSS SARIF v2.1.0 Gatekeeper & Tree Digest on ${GIT_TAG} ==="
if [[ ! -f "${SRC_DIR}/security/sarif-audit-report.sarif.json" ]]; then
  echo "ERRO FATAL DE SEGURANCA (SARIF GATE): O relatorio 'security/sarif-audit-report.sarif.json' nao existe na branch/tag '${GIT_TAG}' de ${SOLUTION_REPO}! Deploy bloqueado!" >&2
  exit 1
fi
if [[ -f "${SRC_DIR}/security/run_oss_sarif_scanners.py" ]]; then
  python3 "${SRC_DIR}/security/run_oss_sarif_scanners.py" --root "${SRC_DIR}" --verify
else
  python3 - "${SRC_DIR}/security/sarif-audit-report.sarif.json" <<'PY'
import json, pathlib, sys
sarif = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if sarif.get("version") != "2.1.0":
    raise SystemExit("ERRO SARIF: version != 2.1.0")
runs = sarif.get("runs", [])
blocking = [
    r for r in (runs[0].get("results", []) if runs else [])
    if str(r.get("level", "")).lower() in ("error", "high", "critical")
]
if blocking:
    raise SystemExit(f"BLOQUEIO SARIF: {len(blocking)} vulnerabilidade(s) critica(s) na tag!")
print("[SARIF-GATE] Relatorio SARIF v2.1.0 validado na branch gcp/sandbox (0 vulnerabilidades bloqueantes).")
PY
fi

# FinOps: servidores de banco singleton sem nenhum workload são PARADOS pelo idle reaper
# (scripts/reap_idle_shared_backing.py, activation-policy=NEVER). Antes do build já disparamos o
# religamento (activation-policy=ALWAYS) para que a partida (alguns minutos) corra em paralelo ao
# Cloud Build; o passo [4/8] espera o servidor ficar RUNNABLE antes do Terraform.
SHARED_DB_RESUME_DRY_RUN="${SHARED_DB_RESUME_DRY_RUN:-false}"
SHARED_DB_RESUME_TIMEOUT_SECONDS="${SHARED_DB_RESUME_TIMEOUT_SECONDS:-600}"
echo "=== [2.8/8] FinOps: religando o servidor de banco singleton (${DATABASE_TYPE}) se estiver parado por ociosidade ==="
python3 "${APPROVALS_ROOT}/scripts/reap_idle_shared_backing.py" resume \
  --project="${TARGET_GCP_PROJECT}" \
  --engine="${DATABASE_TYPE}" \
  --enable-redis="${ENABLE_REDIS,,}" \
  --region="${REGION}" \
  --phase=begin \
  --dry-run="${SHARED_DB_RESUME_DRY_RUN}" \
  || echo "::warning::[SHARED-DB-RESUME] Verificação antecipada falhou; será repetida antes do Terraform."

echo "=== [3/8] Building Backend/API Container(s) & Static SPA GCS Bundle in Cloud Build ==="
API_IMAGE="${AR_REPO}/${WORKLOAD_SLUG}-api:${SHORT_SHA}"
BFF_IMAGE="${AR_REPO}/${WORKLOAD_SLUG}-bff:${SHORT_SHA}"
SPA_STAGE_GCS_URI="gs://${TF_BUCKET}/spa-artifacts/${WORKLOAD_SLUG}-${SHORT_SHA}.tar.gz"

if [[ -f "${SRC_DIR}/deploy/gcp/cloudbuild.yaml" ]]; then
  SUBS="_API_IMAGE=${API_IMAGE},_SPA_STAGE_GCS_URI=${SPA_STAGE_GCS_URI}"
  if grep -q "_BFF_IMAGE" "${SRC_DIR}/deploy/gcp/cloudbuild.yaml"; then
    SUBS="${SUBS},_BFF_IMAGE=${BFF_IMAGE}"
  fi
  gcloud builds submit "${SRC_DIR}" \
    --project="${TARGET_GCP_PROJECT}" \
    --config="${SRC_DIR}/deploy/gcp/cloudbuild.yaml" \
    --substitutions="${SUBS}" \
    --quiet
elif [[ -f "${SRC_DIR}/Dockerfile" ]]; then
  gcloud builds submit "${SRC_DIR}" \
    --project="${TARGET_GCP_PROJECT}" \
    --tag="${API_IMAGE}" \
    --quiet
else
  echo "ERRO: Nenhum deploy/gcp/cloudbuild.yaml ou Dockerfile encontrado em ${SOLUTION_REPO}@${GIT_TAG}." >&2
  exit 1
fi

echo "=== [4/8] Initializing Terraform & Reconciling Shared Singleton GCLB / DB / Redis State ==="
# Religamento automático (FinOps), ANTES de publicar bundle/vars no GCS (um timeout aqui não deixa um
# bundle novo com vars.json antigo para o teardown): o google_sql_database da aplicação só pode ser criado
# com o servidor singleton em execução. Se ele foi parado por ociosidade (activation-policy=NEVER),
# religamos (ALWAYS) e esperamos RUNNABLE; um Redis ainda em remoção (DELETING) é aguardado até sumir,
# para o Terraform recriá-lo. Uma instância parada continua "existente" para o reconcile_shared_state.sh
# (gcloud sql instances describe responde normalmente), que importa o state como sempre; o Terraform gerado
# ignora `settings` (lifecycle.ignore_changes), então não desfaz o activation_policy.
python3 "${APPROVALS_ROOT}/scripts/reap_idle_shared_backing.py" resume \
  --project="${TARGET_GCP_PROJECT}" \
  --engine="${DATABASE_TYPE}" \
  --enable-redis="${ENABLE_REDIS,,}" \
  --region="${REGION}" \
  --phase=wait \
  --timeout-seconds="${SHARED_DB_RESUME_TIMEOUT_SECONDS}" \
  --dry-run="${SHARED_DB_RESUME_DRY_RUN}"

TF_BUNDLE="/tmp/tf-bundle-${WORKLOAD_SLUG}.tar.gz"
tar -czf "${TF_BUNDLE}" -C "${SRC_DIR}" infra/terraform functions 2>/dev/null || tar -czf "${TF_BUNDLE}" -C "${SRC_DIR}" infra/terraform
gcloud storage cp "${TF_BUNDLE}" "gs://${TF_BUCKET}/workloads/${WORKLOAD_SLUG}/tf-bundle.tar.gz" --project="${TARGET_GCP_PROJECT}" --quiet
rm -fv "${TF_BUNDLE}" >/dev/null

VARS_JSON="/tmp/tf-vars-${WORKLOAD_SLUG}.json"
NOW_PRE="$(date -u +%s)"
# Limite superior usado só no rótulo FinOps: o TTL real começa após a validação HTTPS (passo 7/8).
EXP_PRE="$((NOW_PRE + (TTL_MINUTES + SSL_WAIT_MAX_MINUTES + 20) * 60))"

python3 - "${VARS_JSON}" <<PY
import json, os, sys
emails = [e.strip() for e in os.environ["ALERT_EMAILS_CSV"].split(",") if e.strip()]
budget = float(os.environ["BUDGET_BRL"])
ttl_safe = "".join(c if c.isalnum() or c in "_-" else "-" for c in os.environ["TTL_LABEL"].lower())
payload = {
    "project_id": os.environ["TARGET_GCP_PROJECT"],
    "region": "${REGION}",
    "workload_slug": os.environ["WORKLOAD_SLUG"],
    "database_type": os.environ["DATABASE_TYPE"],
    "enable_redis": os.environ["ENABLE_REDIS"].lower() == "true",
    "shared_db_already_exists": False,
    "shared_redis_already_exists": False,
    "shared_gclb_already_exists": False,
    "budget_brl": budget,
    "budget_brl_label": str(int(round(budget))),
    "ttl_label": ttl_safe,
    "ttl_minutes": int(os.environ["TTL_MINUTES"]),
    "expires_at_epoch": "${EXP_PRE}",
    "alert_emails": emails,
    "slo_availability_goal": float(os.environ["SLO_AVAILABILITY_GOAL"]),
    "slo_latency_p95_ms": int(os.environ["SLO_LATENCY_P95_MS"]),
    "api_image": "${API_IMAGE}",
    "bff_image": "${BFF_IMAGE}",
}
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(payload, fh, indent=2)
    fh.write("\n")
PY

terraform -chdir="${SRC_DIR}/infra/terraform" init \
  -backend-config="bucket=${TF_BUCKET}" \
  -backend-config="prefix=workloads/${WORKLOAD_SLUG}" \
  -input=false

if [[ -f "${SRC_DIR}/infra/terraform/reconcile_shared_state.sh" ]]; then
  chmod +x "${SRC_DIR}/infra/terraform/reconcile_shared_state.sh"
  "${SRC_DIR}/infra/terraform/reconcile_shared_state.sh" \
    "${TARGET_GCP_PROJECT}" \
    "${REGION}" \
    "${SRC_DIR}/infra/terraform" \
    "${VARS_JSON}"
fi

gcloud storage cp "${VARS_JSON}" "gs://${TF_BUCKET}/workloads/${WORKLOAD_SLUG}/vars.json" --project="${TARGET_GCP_PROJECT}" --quiet

echo "=== [5/8] Applying Shared Singletons + GCLB .nip.io + SPA GCS Bucket + Cloud Run v2 + Pub/Sub + SRE Terraform ==="
terraform -chdir="${SRC_DIR}/infra/terraform" apply \
  -auto-approve \
  -input=false \
  -var-file="${VARS_JSON}"

TF_OUT_JSON="$(terraform -chdir="${SRC_DIR}/infra/terraform" output -json)"
rm -fv "${VARS_JSON}" >/dev/null

WEB_URL="$(echo "${TF_OUT_JSON}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("web_url",{}).get("value",""))')"
BFF_URL="$(echo "${TF_OUT_JSON}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("bff_url",{}).get("value",""))')"
API_URL="$(echo "${TF_OUT_JSON}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("api_url",{}).get("value",""))')"
GCLB_IP="$(echo "${TF_OUT_JSON}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("gclb_ip",{}).get("value",""))')"
SHORT_SLUG="$(echo "${TF_OUT_JSON}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("short_slug",{}).get("value",""))')"
SPA_BUCKET="$(echo "${TF_OUT_JSON}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("spa_gcs_bucket",{}).get("value",""))')"
API_PREFIX_FLAG="$(echo "${TF_OUT_JSON}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(str(d.get("api_uses_global_prefix",{}).get("value",True)).lower())')"
SEP_BFF_FLAG="$(echo "${TF_OUT_JSON}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(str(d.get("has_separate_bff",{}).get("value",False)).lower())')"
DASHBOARD_URL="$(echo "${TF_OUT_JSON}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("monitoring_dashboard_url",{}).get("value",""))')"
SHARED_SQL="$(echo "${TF_OUT_JSON}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("shared_sql_instance",{}).get("value",""))')"
SHARED_REDIS="$(echo "${TF_OUT_JSON}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("shared_redis_instance",{}).get("value",""))')"

echo "=== [6/8] Publishing Static SPA Assets to GCS BackendBucket & Registering .nip.io Routes on GCLB ==="
if [[ -n "${SPA_BUCKET}" ]]; then
  SPA_TAR="/tmp/spa-${WORKLOAD_SLUG}.tar.gz"
  if gcloud storage cp "${SPA_STAGE_GCS_URI}" "${SPA_TAR}" --project="${TARGET_GCP_PROJECT}" --quiet 2>/dev/null; then
    tar -xzf "${SPA_TAR}" -C "${SPA_EXTRACT_DIR}"
    rm -fv "${SPA_TAR}" >/dev/null
    gcloud storage cp --recursive --cache-control="public, max-age=31536000, immutable" "${SPA_EXTRACT_DIR}"/* "gs://${SPA_BUCKET}/" --project="${TARGET_GCP_PROJECT}" --quiet
    if [[ -f "${SPA_EXTRACT_DIR}/index.html" ]]; then
      gcloud storage cp --cache-control="no-cache, no-store, must-revalidate" "${SPA_EXTRACT_DIR}/index.html" "gs://${SPA_BUCKET}/index.html" --project="${TARGET_GCP_PROJECT}" --quiet
    fi
  fi
fi

if [[ -n "${GCLB_IP}" && -n "${SHORT_SLUG}" ]]; then
  python3 "${APPROVALS_ROOT}/scripts/reconcile_shared_gclb_routes.py" upsert \
    --project="${TARGET_GCP_PROJECT}" \
    --short-slug="${SHORT_SLUG}" \
    --gclb-ip="${GCLB_IP}" \
    --api-uses-global-prefix="${API_PREFIX_FLAG}" \
    --has-separate-bff="${SEP_BFF_FLAG}"
fi

echo "=== [7/8] Validating managed SSL certificate & HTTPS routes before starting the TTL clock (max ${SSL_WAIT_MAX_MINUTES} min) ==="
DEPLOYED_EPOCH="$(date -u +%s)"
DEPLOYED_AT_UTC="$(date -u -d "@${DEPLOYED_EPOCH}" +"%Y-%m-%dT%H:%M:%SZ")"
CERT_NAME="${SHORT_SLUG}-nip-cert"
FALLBACK_MARKER="Enterprise Sandbox GCLB"
HTTPS_VERIFIED="false"
HTTPS_VERIFIED_AT_UTC=""
LAST_CERT_STATE=""
CONSECUTIVE_OK=0
REQUIRED_OK=3

# Uma rodada só passa se os 3 hosts responderem com TLS confiável (curl sem -k), o frontend
# devolver 200 com a SPA (não a página de fallback do GCLB) e API/BFF responderem pela app (< 500).
https_round_ok() {
  local body code url
  body="$(mktemp)"
  code="$(curl -sS -o "${body}" -w '%{http_code}' --max-time 20 "${WEB_URL}/" 2>/dev/null || true)"
  if [[ "${code}" != "200" ]] || grep -q "${FALLBACK_MARKER}" "${body}"; then
    rm -fv "${body}" >/dev/null
    return 1
  fi
  for url in "${API_URL}" "${BFF_URL}"; do
    [[ -z "${url}" ]] && continue
    code="$(curl -sS -o "${body}" -w '%{http_code}' --max-time 20 "${url}/" 2>/dev/null || true)"
    if [[ ! "${code}" =~ ^[1-4][0-9][0-9]$ ]] || grep -q "${FALLBACK_MARKER}" "${body}"; then
      rm -fv "${body}" >/dev/null
      return 1
    fi
  done
  rm -fv "${body}" >/dev/null
  return 0
}

if [[ -z "${WEB_URL}" ]]; then
  echo "::warning::[SSL] web_url ausente nos outputs do Terraform; o TTL começa sem validação HTTPS."
else
  while true; do
    CERT_STATE="$(gcloud certificate-manager certificates describe "${CERT_NAME}" --location=global --project="${TARGET_GCP_PROJECT}" --format='value(managed.state)' 2>/dev/null || echo "UNKNOWN")"
    if [[ "${CERT_STATE}" != "${LAST_CERT_STATE}" ]]; then
      echo "[SSL] $(date -u +%H:%M:%S) certificado ${CERT_NAME}: ${CERT_STATE:-UNKNOWN}"
      LAST_CERT_STATE="${CERT_STATE}"
    fi
    if https_round_ok; then
      CONSECUTIVE_OK=$((CONSECUTIVE_OK + 1))
      echo "[SSL] $(date -u +%H:%M:%S) HTTPS com certificado confiável respondendo nos 3 hosts (${CONSECUTIVE_OK}/${REQUIRED_OK})"
      if [[ "${CONSECUTIVE_OK}" -ge "${REQUIRED_OK}" ]]; then
        HTTPS_VERIFIED="true"
        break
      fi
      sleep 15
      continue
    fi
    CONSECUTIVE_OK=0
    if [[ "$(( $(date -u +%s) - DEPLOYED_EPOCH ))" -ge "$(( SSL_WAIT_MAX_MINUTES * 60 ))" ]]; then
      break
    fi
    sleep 20
  done
fi

TTL_START_EPOCH="$(date -u +%s)"
TTL_STARTED_AT_UTC="$(date -u -d "@${TTL_START_EPOCH}" +"%Y-%m-%dT%H:%M:%SZ")"
SSL_WAIT_MINUTES="$(( (TTL_START_EPOCH - DEPLOYED_EPOCH + 59) / 60 ))"
if [[ "${HTTPS_VERIFIED}" == "true" ]]; then
  HTTPS_VERIFIED_AT_UTC="${TTL_STARTED_AT_UTC}"
  echo "[SSL] HTTPS validado em ${HTTPS_VERIFIED_AT_UTC} (${SSL_WAIT_MINUTES} min após o deploy). O relógio do TTL (${TTL_LABEL}) começa agora."
else
  echo "::error::[SSL] Certificado/rotas HTTPS não validados em ${SSL_WAIT_MAX_MINUTES} min (certificado ${CERT_NAME}: ${LAST_CERT_STATE:-UNKNOWN}). Por segurança o TTL começa agora e a aplicação será removida ao final do prazo."
fi

echo "=== [8/8] Recording Active Lease & Exact TTL Timer (${TTL_LABEL}, iniciado após a validação HTTPS) ==="
mkdir -p "${APPROVALS_ROOT}/active-leases"
EXPIRES_AT_EPOCH="$((TTL_START_EPOCH + TTL_MINUTES * 60))"
EXPIRES_AT_UTC="$(date -u -d "@${EXPIRES_AT_EPOCH}" +"%Y-%m-%dT%H:%M:%SZ")"
cat <<EOF > "${APPROVALS_ROOT}/active-leases/${WORKLOAD_SLUG}.json"
{
  "workload_slug": "${WORKLOAD_SLUG}",
  "short_slug": "${SHORT_SLUG}",
  "status": "ACTIVE",
  "target_gcp_project": "${TARGET_GCP_PROJECT}",
  "region": "${REGION}",
  "github_org": "${GITHUB_ORG}",
  "solution_repo": "${SOLUTION_REPO}",
  "git_tag": "${GIT_TAG}",
  "deployed_commit_sha": "${TAG_COMMIT_SHA}",
  "commits_ahead_of_main": ${AHEAD_MAIN},
  "commits_behind_main": ${BEHIND_MAIN},
  "compare_against_main_url": "${COMPARE_URL}",
  "gclb_ip": "${GCLB_IP}",
  "spa_gcs_bucket": "${SPA_BUCKET}",
  "database_type": "${DATABASE_TYPE}",
  "shared_sql_instance": "${SHARED_SQL}",
  "shared_redis_instance": "${SHARED_REDIS}",
  "enable_redis": $([[ "${ENABLE_REDIS,,}" == "true" ]] && echo true || echo false),
  "alert_emails": "${ALERT_EMAILS_CSV}",
  "monitoring_dashboard_url": "${DASHBOARD_URL}",
  "budget_brl": ${BUDGET_BRL},
  "ttl": "${TTL_LABEL}",
  "ttl_minutes": ${TTL_MINUTES},
  "deployed_at_utc": "${DEPLOYED_AT_UTC}",
  "https_verified": ${HTTPS_VERIFIED},
  "https_verified_at_utc": "${HTTPS_VERIFIED_AT_UTC}",
  "ssl_wait_minutes": ${SSL_WAIT_MINUTES},
  "ttl_started_at_utc": "${TTL_STARTED_AT_UTC}",
  "expires_at_utc": "${EXPIRES_AT_UTC}",
  "expires_at_epoch": ${EXPIRES_AT_EPOCH},
  "web_url": "${WEB_URL}",
  "bff_url": "${BFF_URL}",
  "api_url": "${API_URL}",
  "teardown_mode": "terraform_destroy_workload_only",
  "terraform_state_uri": "gs://${TF_BUCKET}/workloads/${WORKLOAD_SLUG}"
}
EOF

CRON_EXPR="$(python3 - "${EXPIRES_AT_EPOCH}" <<'PY'
import datetime, sys
dt = datetime.datetime.fromtimestamp(int(sys.argv[1]) + 60, tz=datetime.timezone.utc)
print(f"{dt.minute} {dt.hour} {dt.day} {dt.month} *")
PY
)"
SCHED_JOB="ttl-exp-${WORKLOAD_SLUG}"
TRIGGER_URI="https://api.github.com/repos/${GITHUB_ORG}/cloud-provisioning-approvals/contents/teardown-triggers/${WORKLOAD_SLUG}-${EXPIRES_AT_EPOCH}.json"
TRIGGER_CONTENT_B64="$(printf '{"workload_slug":"%s","expires_at_utc":"%s","ttl":"%s"}\n' "${WORKLOAD_SLUG}" "${EXPIRES_AT_UTC}" "${TTL_LABEL}" | base64 -w 0)"
TRIGGER_BODY="$(printf '{"message":"chore(ttl-trigger): auto-expire %s after %s","branch":"main","content":"%s"}' "${WORKLOAD_SLUG}" "${TTL_LABEL}" "${TRIGGER_CONTENT_B64}")"

gcloud scheduler jobs delete "${SCHED_JOB}" --location="${REGION}" --project="${TARGET_GCP_PROJECT}" --quiet >/dev/null 2>&1 || true
gcloud scheduler jobs create http "${SCHED_JOB}" \
  --location="${REGION}" \
  --project="${TARGET_GCP_PROJECT}" \
  --schedule="${CRON_EXPR}" \
  --time-zone="Etc/UTC" \
  --uri="${TRIGGER_URI}" \
  --http-method=PUT \
  --headers="Authorization=Bearer ${GH_PAT},Accept=application/vnd.github+json,Content-Type=application/json,X-GitHub-Api-Version=2022-11-28,User-Agent=sdlc-ttl-scheduler" \
  --message-body="${TRIGGER_BODY}" \
  --quiet || echo "Aviso: Cloud Scheduler secundario nao criado; o cron do GitHub Actions cobrira o TTL."

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  {
    echo "web_url=${WEB_URL}"
    echo "bff_url=${BFF_URL}"
    echo "api_url=${API_URL}"
    echo "dashboard_url=${DASHBOARD_URL}"
    echo "shared_sql_instance=${SHARED_SQL}"
    echo "deployed_at_utc=${DEPLOYED_AT_UTC}"
    echo "https_verified=${HTTPS_VERIFIED}"
    echo "https_verified_at_utc=${HTTPS_VERIFIED_AT_UTC}"
    echo "ttl_started_at_utc=${TTL_STARTED_AT_UTC}"
    echo "ssl_wait_minutes=${SSL_WAIT_MINUTES}"
    echo "expires_at_utc=${EXPIRES_AT_UTC}"
    echo "compare_url=${COMPARE_URL}"
    echo "ahead_main=${AHEAD_MAIN}"
    echo "behind_main=${BEHIND_MAIN}"
  } >> "${GITHUB_OUTPUT}"
fi

echo "DEPLOY CONCLUIDO: web=${WEB_URL} | bff=${BFF_URL} | api=${API_URL} | dashboard=${DASHBOARD_URL} | HTTPS validado=${HTTPS_VERIFIED} (${HTTPS_VERIFIED_AT_UTC:-n/a}) | TTL=${TTL_LABEL} iniciado em ${TTL_STARTED_AT_UTC} (expira em ${EXPIRES_AT_UTC})"
if [[ "${HTTPS_VERIFIED}" != "true" ]]; then
  echo "ERRO: deploy sem validação HTTPS dentro de ${SSL_WAIT_MAX_MINUTES} min; lease e TTL registrados para garantir a remoção automática." >&2
  exit 1
fi
