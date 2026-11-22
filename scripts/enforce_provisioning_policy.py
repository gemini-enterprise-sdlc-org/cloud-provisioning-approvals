#!/usr/bin/env python3
"""Deterministic FinOps, Shared Singleton DB, SRE Alerting & Tag Lock Gatekeeper.

Enforces:
  1. Mandatory `budget_brl` (> 0) and `ttl` (e.g. '1h' demo / '15d' sandbox).
  2. Mandatory `observability_sre.alert_emails` (>= 1 valid recipient e-mail for Cloud Monitoring alerts).
  3. Valid singleton `database_type` ('postgresql', 'mysql', 'sqlserver', or 'none').
  4. Dual-pinning on `GCP_DEPLOYED_VERSION` tag + immutable `expected_commit_sha` (anti-TOCTOU).
  5. Required `PRD.md` (greenfield) or `ARCHITECTURE_REVIEW.md` (brownfield) + `APPROVAL_REQUEST.md`.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import hmac
import json
import os
import pathlib
import re
import sys
from typing import Any

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}[a-z0-9]$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{2,80}$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_TTL_RE = re.compile(
    r"^\s*(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)\s*$",
    re.IGNORECASE,
)


def parse_ttl_to_minutes(raw_ttl: str) -> int:
    match = _TTL_RE.fullmatch(str(raw_ttl or ""))
    if not match:
        raise ValueError(
            f"Formato de TTL inválido '{raw_ttl}'. Use minutos ('30m'), horas ('1h') ou dias ('15d')."
        )
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith("m"):
        return amount
    if unit.startswith("h"):
        return amount * 60
    return amount * 1440


def load_policy(repo_root: pathlib.Path) -> dict[str, Any]:
    policy_path = repo_root / "policies" / "governance-policy.json"
    if not policy_path.is_file():
        raise FileNotFoundError(f"Arquivo de política ausente: {policy_path}")
    return json.loads(policy_path.read_text(encoding="utf-8"))


def validate_manifest(manifest_path: pathlib.Path, policy: dict[str, Any]) -> dict[str, Any]:
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Manifesto de aprovação não encontrado: {manifest_path}")

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    req_dir = manifest_path.parent

    workload_slug = str(data.get("workload_slug", "")).strip()
    if not _SLUG_RE.fullmatch(workload_slug):
        errors.append(f"workload_slug inválido: '{workload_slug}'.")

    request_type = str(data.get("request_type", "")).strip()
    if request_type not in {"greenfield_new_project", "brownfield_repo_review"}:
        errors.append(
            f"request_type inválido '{request_type}'. Esperado 'greenfield_new_project' ou 'brownfield_repo_review'."
        )

    approval_doc = req_dir / "APPROVAL_REQUEST.md"
    if not approval_doc.is_file() or len(approval_doc.read_text(encoding="utf-8").strip()) < 80:
        errors.append(f"Documento obrigatório APPROVAL_REQUEST.md ausente ou incompleto em {req_dir}.")

    if request_type == "greenfield_new_project":
        prd_doc = req_dir / "PRD.md"
        if not prd_doc.is_file() or len(prd_doc.read_text(encoding="utf-8").strip()) < 80:
            errors.append(f"Documento obrigatório PRD.md ausente ou incompleto para novo projeto em {req_dir}.")
    elif request_type == "brownfield_repo_review":
        rev_doc = req_dir / "ARCHITECTURE_REVIEW.md"
        if not rev_doc.is_file() or len(rev_doc.read_text(encoding="utf-8").strip()) < 80:
            errors.append(
                f"Documento obrigatório ARCHITECTURE_REVIEW.md ausente ou incompleto para revisão de repo existente em {req_dir}."
            )

    raw_budget = data.get("budget_brl")
    budget_brl = 0.0
    if raw_budget is None or str(raw_budget).strip() == "":
        errors.append("Argumento obrigatório 'budget_brl' ausente no manifesto de aprovação.")
    else:
        try:
            budget_brl = float(raw_budget)
            min_b = float(policy.get("min_budget_brl", 1.0))
            max_b = float(policy.get("max_sandbox_budget_brl", 5000.0))
            if budget_brl < min_b or budget_brl > max_b:
                errors.append(
                    f"budget_brl ({budget_brl:.2f}) fora da faixa permitida [{min_b:.2f}, {max_b:.2f}] BRL."
                )
        except (TypeError, ValueError):
            errors.append(f"budget_brl inválido: '{raw_budget}'.")

    raw_ttl = str(data.get("ttl", "")).strip()
    ttl_minutes = 0
    if not raw_ttl:
        errors.append("Argumento obrigatório 'ttl' (ex.: '1h' ou '15d') ausente no manifesto de aprovação.")
    else:
        try:
            ttl_minutes = parse_ttl_to_minutes(raw_ttl)
            min_ttl = int(policy.get("min_ttl_minutes", 5))
            max_ttl = int(policy.get("max_ttl_minutes", 43200))
            if ttl_minutes < min_ttl or ttl_minutes > max_ttl:
                errors.append(
                    f"ttl '{raw_ttl}' ({ttl_minutes} min) fora do intervalo permitido [{min_ttl}, {max_ttl}] minutos."
                )
        except ValueError as exc:
            errors.append(str(exc))

    declared_ttl_min = data.get("ttl_minutes")
    if declared_ttl_min is not None and ttl_minutes > 0 and int(declared_ttl_min) != ttl_minutes:
        errors.append(
            f"Divergência entre ttl ('{raw_ttl}' -> {ttl_minutes} min) e ttl_minutes declarado ({declared_ttl_min})."
        )

    database_type = str(data.get("database_type", "postgresql")).strip().lower()
    if database_type not in {"postgresql", "mysql", "sqlserver", "none"}:
        errors.append(
            f"database_type inválido '{database_type}'. Permitidos: postgresql, mysql, sqlserver, none."
        )
    enable_redis = bool(data.get("enable_redis", False))

    obs = data.get("observability_sre") or {}
    raw_emails = obs.get("alert_emails") if isinstance(obs, dict) else []
    alert_emails = [str(e).strip().lower() for e in (raw_emails or []) if str(e).strip()]
    if not alert_emails or any(not _EMAIL_RE.fullmatch(e) for e in alert_emails):
        errors.append(
            "observability_sre.alert_emails obrigatório com ao menos 1 endereço de e-mail válido para alertas Cloud Monitoring."
        )
    slo_avail = float(obs.get("slo_availability_goal", 0.995) if isinstance(obs, dict) else 0.995)
    slo_lat_ms = int(obs.get("slo_latency_p95_ms", 1500) if isinstance(obs, dict) else 1500)

    target_project = str(data.get("target_gcp_project", "")).strip()
    allowed_projects = set(policy.get("allowed_gcp_projects", ["ernani-demo-argolis"]))
    if target_project not in allowed_projects:
        errors.append(
            f"target_gcp_project '{target_project}' não autorizado. Permitidos: {sorted(allowed_projects)}."
        )

    sa_email = str(data.get("provisioner_service_account", "")).strip()
    expected_sa = str(policy.get("provisioner_service_account", "")).strip()
    if sa_email != expected_sa:
        errors.append(
            f"provisioner_service_account inválida ('{sa_email}'). Obrigatório usar '{expected_sa}'."
        )

    if str(data.get("teardown_mode", "")).strip() != "terraform_destroy_workload_only":
        errors.append("teardown_mode deve ser estritamente 'terraform_destroy_workload_only'.")

    github_org = str(data.get("github_org", "")).strip()
    if github_org != str(policy.get("allowed_github_org", "gemini-enterprise-sdlc-org")):
        errors.append(f"github_org inválida '{github_org}'.")

    solution_repo = str(data.get("solution_repo", "")).strip()
    reserved_repos = set(
        policy.get(
            "reserved_protected_repos",
            ["cloud-provisioning-approvals", "sdlc-platform-skills"],
        )
    )
    if not _REPO_RE.fullmatch(solution_repo) or solution_repo in reserved_repos:
        errors.append(
            f"solution_repo inválido ou reservado '{solution_repo}': proibido usar repositórios de governança ({sorted(reserved_repos)})."
        )

    required_tag = str(policy.get("required_git_tag", "GCP_DEPLOYED_VERSION")).strip()
    git_tag = str(data.get("git_tag", "")).strip()
    if git_tag != required_tag:
        errors.append(
            f"git_tag inválida '{git_tag}': obrigatório apontar o deployment para a tag '{required_tag}'."
        )

    expected_sha = str(
        data.get("expected_commit_sha") or data.get("source_commit_sha") or ""
    ).strip()
    if not _SHA_RE.fullmatch(expected_sha):
        errors.append(
            f"expected_commit_sha inválido '{expected_sha}' (exigido hash hexadecimal Git para trava anti-tampering)."
        )

    if str(data.get("sarif_security_verdict", "")).strip().upper() != "PASS":
        errors.append("sarif_security_verdict deve ser 'PASS' antes de aprovar provisionamento.")

    approval_status = str(data.get("approval_status", "")).strip().upper()
    if approval_status not in {"PENDING_APPROVAL", "APPROVED_ON_MERGE", "APPROVED", "REJECTED"}:
        errors.append(f"approval_status inválido '{approval_status}'.")

    seal = data.get("integrity_seal")
    if not isinstance(seal, dict):
        errors.append("VIOLAÇÃO DE INTEGRIDADE (ANTI-DRIFT): Bloco obrigatório 'integrity_seal' ausente em approval-manifest.json (o arquivo deve ser gerado exclusivamente pelo Agente ADK).")
    else:
        seal_fields = seal.get("canonical_fields") or [
            "schema_version", "workload_slug", "request_type", "github_org", "solution_repo",
            "git_branch", "git_tag", "expected_commit_sha", "target_gcp_project", "region",
            "provisioner_service_account", "budget_brl", "ttl", "ttl_minutes", "database_type",
            "enable_redis", "observability_sre", "teardown_mode", "sarif_security_verdict",
        ]
        canonical_payload = json.dumps(
            {k: data.get(k) for k in seal_fields},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        computed_canonical_sha = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
        if computed_canonical_sha != seal.get("canonical_payload_sha256"):
            errors.append(
                f"VIOLAÇÃO DE INTEGRIDADE (ANTI-DRIFT): Os parâmetros de governança em approval-manifest.json (sha256={computed_canonical_sha[:12]}) divergem do selo original do Agente ({str(seal.get('canonical_payload_sha256'))[:12]})! Edição manual fora do Agente bloqueada."
            )
        comp_doc_name = "PRD.md" if request_type == "greenfield_new_project" else "ARCHITECTURE_REVIEW.md"
        comp_doc = req_dir / comp_doc_name
        if comp_doc.is_file() and approval_doc.is_file():
            computed_docs_sha = hashlib.sha256(
                (comp_doc.read_text(encoding="utf-8").strip() + "\n---\n" + approval_doc.read_text(encoding="utf-8").strip()).encode("utf-8")
            ).hexdigest()
            if seal.get("companion_docs_sha256") and computed_docs_sha != seal.get("companion_docs_sha256"):
                errors.append(
                    "VIOLAÇÃO DE INTEGRIDADE (ANTI-DRIFT): Os documentos APPROVAL_REQUEST.md / ARCHITECTURE_REVIEW.md foram modificados manualmente após a emissão do selo pelo Agente!"
                )
        env_hmac_key = os.environ.get("MANIFEST_HMAC_SECRET", "").strip()
        if env_hmac_key:
            expected_hmac = hmac.new(
                env_hmac_key.encode("utf-8"),
                f"{computed_canonical_sha}:{seal.get('companion_docs_sha256', '')}:{expected_sha}".encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected_hmac, str(seal.get("hmac_sha256_signature", ""))):
                errors.append(
                    "VIOLAÇÃO CRIPTOGRÁFICA HMAC-SHA256: A assinatura 'hmac_sha256_signature' do manifesto é inválida!"
                )

    if errors:
        raise ValueError("Falha na validação determinística de governança:\n- " + "\n- ".join(errors))

    now = datetime.datetime.now(datetime.timezone.utc)
    expires_at = now + datetime.timedelta(minutes=ttl_minutes)
    compare_url = f"https://github.com/{github_org}/{solution_repo}/compare/main...{git_tag}"
    return {
        "workload_slug": workload_slug,
        "request_type": request_type,
        "github_org": github_org,
        "solution_repo": solution_repo,
        "git_tag": git_tag,
        "git_branch": str(data.get("git_branch", "gcp/sandbox")).strip(),
        "expected_commit_sha": expected_sha,
        "source_commit_sha": expected_sha,
        "compare_url": compare_url,
        "target_gcp_project": target_project,
        "provisioner_service_account": sa_email,
        "budget_brl": f"{budget_brl:.2f}",
        "ttl": raw_ttl,
        "ttl_minutes": ttl_minutes,
        "database_type": database_type,
        "enable_redis": enable_redis,
        "alert_emails_csv": ",".join(alert_emails),
        "slo_availability_goal": slo_avail,
        "slo_latency_p95_ms": slo_lat_ms,
        "approval_status": approval_status,
        "allow_deploy": approval_status in {"APPROVED_ON_MERGE", "APPROVED", "PENDING_APPROVAL"},
        "expires_at_epoch": int(expires_at.timestamp()),
        "expires_at_utc": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Enforce provisioning governance policy.")
    parser.add_argument("--mode", choices=["validate-pr", "pre-deploy"], required=True)
    parser.add_argument("--manifest", required=True, help="Path to approval-manifest.json")
    parser.add_argument("--github-output", default="", help="Optional path to $GITHUB_OUTPUT")
    args = parser.parse_args()

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    policy = load_policy(repo_root)
    manifest_path = pathlib.Path(args.manifest).resolve()

    validated = validate_manifest(manifest_path, policy)
    if args.mode == "pre-deploy" and validated["approval_status"] == "REJECTED":
        validated["allow_deploy"] = False
        print(
            f"[GOVERNANCE GATE] Solicitação '{validated['workload_slug']}' marcada como REJECTED. "
            "Provisionamento bloqueado."
        )
    else:
        print(
            f"[GOVERNANCE GATE PASS] workload={validated['workload_slug']} "
            f"repo={validated['github_org']}/{validated['solution_repo']}@{validated['git_tag']} "
            f"(sha={validated['expected_commit_sha'][:8]}, db={validated['database_type']}, "
            f"alerts={validated['alert_emails_csv']}) "
            f"budget_brl=R$ {validated['budget_brl']} ttl={validated['ttl']}"
        )

    if args.github_output:
        out_path = pathlib.Path(args.github_output)
        with out_path.open("a", encoding="utf-8") as fh:
            for k, v in validated.items():
                val_str = "true" if v is True else ("false" if v is False else str(v))
                fh.write(f"{k}={val_str}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
