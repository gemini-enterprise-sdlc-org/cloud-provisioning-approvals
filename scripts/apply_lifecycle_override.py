#!/usr/bin/env python3
"""Applies EA/PE lifecycle overrides (early decommission or TTL change) to active sandbox workloads.

Each request is a JSON file in `lifecycle-requests/` written by the ADK agent
(`request_workload_lifecycle_override`). The script:
  1. verifies the HMAC-SHA256 integrity seal (key = Secret Manager `sdlc-github-org-pat`);
  2. checks the requester against `lifecycle_override_approvers` in policies/governance-policy.json;
  3. checks that `active-leases/<slug>.json` is ACTIVE;
  4. decommission: sets `expires_at_epoch = now` and writes `teardown-triggers/<slug>-<now>.json`
     (the push of that file starts the teardown workflow);
     set_ttl: moves `expires_at_epoch` to now + TTL and re-times Cloud Scheduler `ttl-exp-<slug>`;
  5. moves the request to `lifecycle-requests/processed/` with the result (APPLIED / REJECTED).
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import hmac
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from typing import Any

SEAL_FIELDS = (
    "workload_slug",
    "action",
    "ttl",
    "ttl_minutes",
    "requester_email",
    "reason",
    "requested_at_utc",
    "requested_at_epoch",
    "nonce",
)
VALID_ACTIONS = ("decommission", "set_ttl")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def utc_iso(epoch: int) -> str:
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_sha256(request: dict[str, Any]) -> str:
    payload = json.dumps(
        {k: request.get(k) for k in SEAL_FIELDS},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def expected_signature(request: dict[str, Any], key: str) -> str:
    return hmac.new(
        key.encode("utf-8"),
        f"lifecycle:{canonical_sha256(request)}".encode(),
        hashlib.sha256,
    ).hexdigest()


def verify_seal(request: dict[str, Any], key: str) -> str:
    """Returns an error message, or an empty string when the seal is valid."""
    seal = request.get("integrity_seal")
    if not isinstance(seal, dict):
        return "bloco 'integrity_seal' ausente (o pedido deve ser gerado pelo agente)"
    if seal.get("canonical_payload_sha256") != canonical_sha256(request):
        return "conteúdo do pedido alterado após a assinatura (SHA-256 divergente)"
    if not key:
        return "chave HMAC indisponível no runner"
    if not hmac.compare_digest(expected_signature(request, key), str(seal.get("hmac_sha256_signature", ""))):
        return "assinatura HMAC-SHA256 inválida"
    return ""


def load_policy(repo_root: pathlib.Path) -> dict[str, Any]:
    policy_path = repo_root / "policies" / "governance-policy.json"
    return json.loads(policy_path.read_text(encoding="utf-8"))


def recreate_scheduler(lease: dict[str, Any], slug: str, exp_epoch: int, ttl_label: str, gh_pat: str) -> str:
    """Re-times Cloud Scheduler `ttl-exp-<slug>` (same contract as provision_workload.sh). Returns a status note."""
    if not shutil.which("gcloud"):
        return "gcloud indisponível: Cloud Scheduler não reprogramado"
    if not gh_pat:
        return "PAT indisponível: Cloud Scheduler não reprogramado"
    project = str(lease.get("target_gcp_project") or "ernani-demo-argolis")
    region = str(lease.get("region") or "southamerica-east1")
    org = str(lease.get("github_org") or "gemini-enterprise-sdlc-org")
    job = f"ttl-exp-{slug}"
    fire = datetime.datetime.fromtimestamp(exp_epoch + 60, tz=datetime.timezone.utc)
    cron = f"{fire.minute} {fire.hour} {fire.day} {fire.month} *"
    exp_utc = utc_iso(exp_epoch)
    trigger_uri = (
        f"https://api.github.com/repos/{org}/cloud-provisioning-approvals/contents/"
        f"teardown-triggers/{slug}-{exp_epoch}.json"
    )
    content = json.dumps({"workload_slug": slug, "expires_at_utc": exp_utc, "ttl": ttl_label}) + "\n"
    body = json.dumps(
        {
            "message": f"chore(ttl-trigger): auto-expire {slug} after {ttl_label}",
            "branch": "main",
            "content": base64.b64encode(content.encode()).decode("ascii"),
        },
        separators=(",", ":"),
    )
    subprocess.run(
        ["gcloud", "scheduler", "jobs", "delete", job, f"--location={region}", f"--project={project}", "--quiet"],
        check=False,
        capture_output=True,
    )
    proc = subprocess.run(
        [
            "gcloud",
            "scheduler",
            "jobs",
            "create",
            "http",
            job,
            f"--location={region}",
            f"--project={project}",
            f"--schedule={cron}",
            "--time-zone=Etc/UTC",
            f"--uri={trigger_uri}",
            "--http-method=PUT",
            "--headers=Authorization=Bearer "
            + gh_pat
            + ",Accept=application/vnd.github+json,Content-Type=application/json,"
            "X-GitHub-Api-Version=2022-11-28,User-Agent=sdlc-ttl-scheduler",
            f"--message-body={body}",
            "--quiet",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").replace(gh_pat, "***").strip()[:300]
        return f"falha ao reprogramar Cloud Scheduler {job}: {err}"
    return f"Cloud Scheduler {job} reprogramado para {cron} (UTC)"


def process_request(
    repo_root: pathlib.Path,
    req_path: pathlib.Path,
    policy: dict[str, Any],
    hmac_key: str,
    gh_pat: str,
    now_epoch: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {"request_file": req_path.name, "status": "REJECTED", "detail": ""}
    try:
        request = json.loads(req_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        result["detail"] = f"JSON inválido: {exc}"
        return result

    slug = str(request.get("workload_slug", "")).strip().lower()
    action = str(request.get("action", "")).strip().lower()
    requester = str(request.get("requester_email", "")).strip().lower()
    result.update({"workload_slug": slug, "action": action, "requester_email": requester})

    seal_error = verify_seal(request, hmac_key)
    if seal_error:
        result["detail"] = f"VIOLAÇÃO DE INTEGRIDADE: {seal_error}"
        return result
    if not _SLUG_RE.fullmatch(slug):
        result["detail"] = f"workload_slug inválido: '{slug}'"
        return result
    if action not in VALID_ACTIONS:
        result["detail"] = f"ação inválida '{action}' (use {', '.join(VALID_ACTIONS)})"
        return result

    approvers = {str(e).strip().lower() for e in policy.get("lifecycle_override_approvers", []) if str(e).strip()}
    if requester not in approvers:
        result["detail"] = f"'{requester}' não está em lifecycle_override_approvers da política de governança"
        return result

    lease_path = repo_root / "active-leases" / f"{slug}.json"
    if not lease_path.is_file():
        result["detail"] = f"lease active-leases/{slug}.json não encontrado"
        return result
    lease = json.loads(lease_path.read_text(encoding="utf-8"))
    if str(lease.get("status", "")).upper() != "ACTIVE":
        result["detail"] = f"lease de '{slug}' não está ACTIVE (status={lease.get('status')})"
        return result

    overrides = list(lease.get("lifecycle_overrides", []))
    entry = {
        "action": action,
        "requester_email": requester,
        "reason": str(request.get("reason", ""))[:300],
        "requested_at_utc": request.get("requested_at_utc"),
        "applied_at_utc": utc_iso(now_epoch),
        "previous_expires_at_utc": lease.get("expires_at_utc"),
    }

    if action == "decommission":
        lease["expires_at_epoch"] = now_epoch
        lease["expires_at_utc"] = utc_iso(now_epoch)
        lease["decommission_requested_by"] = requester
        lease["decommission_requested_at_utc"] = utc_iso(now_epoch)
        entry["new_expires_at_utc"] = utc_iso(now_epoch)
        overrides.append(entry)
        lease["lifecycle_overrides"] = overrides
        lease_path.write_text(json.dumps(lease, indent=2) + "\n", encoding="utf-8")
        trig_dir = repo_root / "teardown-triggers"
        trig_dir.mkdir(exist_ok=True)
        (trig_dir / f"{slug}-{now_epoch}.json").write_text(
            json.dumps(
                {
                    "workload_slug": slug,
                    "expires_at_utc": utc_iso(now_epoch),
                    "reason": f"DECOMMISSION_REQUESTED ({requester})",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        result.update(
            {
                "status": "APPLIED",
                "detail": "lease expirado agora e teardown-trigger criado (o teardown inicia com este commit)",
                "new_expires_at_utc": utc_iso(now_epoch),
            }
        )
        return result

    # set_ttl
    try:
        ttl_minutes = int(request.get("ttl_minutes", 0))
    except (TypeError, ValueError):
        ttl_minutes = 0
    min_m = int(policy.get("lifecycle_override_min_minutes", 5))
    max_m = int(policy.get("lifecycle_override_max_minutes", 1440))
    if ttl_minutes < min_m or ttl_minutes > max_m:
        result["detail"] = f"TTL de {ttl_minutes} min fora do limite permitido [{min_m}..{max_m}] min"
        return result
    ttl_label = str(request.get("ttl") or f"{ttl_minutes}m")
    new_exp = now_epoch + ttl_minutes * 60
    lease["expires_at_epoch"] = new_exp
    lease["expires_at_utc"] = utc_iso(new_exp)
    lease["ttl"] = ttl_label
    lease["ttl_minutes"] = ttl_minutes
    lease["ttl_overridden_by"] = requester
    lease["ttl_overridden_at_utc"] = utc_iso(now_epoch)
    entry["new_expires_at_utc"] = utc_iso(new_exp)
    entry["ttl"] = ttl_label
    sched_note = recreate_scheduler(lease, slug, new_exp, ttl_label, gh_pat)
    entry["scheduler"] = sched_note
    overrides.append(entry)
    lease["lifecycle_overrides"] = overrides
    lease_path.write_text(json.dumps(lease, indent=2) + "\n", encoding="utf-8")
    result.update(
        {
            "status": "APPLIED",
            "detail": f"novo TTL {ttl_label} a partir de agora; {sched_note}",
            "new_expires_at_utc": utc_iso(new_exp),
        }
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply EA/PE lifecycle overrides to active sandbox workloads.")
    parser.add_argument("--github-output", default="", help="Optional $GITHUB_OUTPUT path")
    parser.add_argument("--commit-message-file", default="/tmp/lifecycle-commit-message.txt")
    args = parser.parse_args()

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    req_dir = repo_root / "lifecycle-requests"
    pending = sorted(p for p in req_dir.glob("*.json") if p.is_file()) if req_dir.is_dir() else []
    if not pending:
        print("Nenhum pedido pendente em lifecycle-requests/.")
        if args.github_output:
            with pathlib.Path(args.github_output).open("a", encoding="utf-8") as fh:
                fh.write("processed_count=0\napplied_count=0\ndecommissioned_slugs=\n")
        return 0

    policy = load_policy(repo_root)
    hmac_key = os.environ.get("LIFECYCLE_HMAC_KEY", "").strip()
    gh_pat = os.environ.get("GH_PAT", "").strip()
    now_epoch = int(datetime.datetime.now(datetime.timezone.utc).timestamp())

    processed_dir = req_dir / "processed"
    processed_dir.mkdir(exist_ok=True)
    results: list[dict[str, Any]] = []
    for req_path in pending:
        try:
            res = process_request(repo_root, req_path, policy, hmac_key, gh_pat, now_epoch)
        except Exception as exc:  # noqa: BLE001 - every request must leave an auditable receipt
            res = {"request_file": req_path.name, "status": "REJECTED", "detail": f"erro inesperado: {exc}"}
        results.append(res)
        try:
            original = json.loads(req_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            original = {"raw_request_file": req_path.name}
        original["processing_result"] = {**res, "processed_at_utc": utc_iso(now_epoch)}
        (processed_dir / req_path.name).write_text(
            json.dumps(original, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        req_path.unlink()
        print(f"[LIFECYCLE] {req_path.name}: {res['status']} — {res.get('detail', '')}")

    applied = [r for r in results if r["status"] == "APPLIED"]
    decommissioned = [r["workload_slug"] for r in applied if r.get("action") == "decommission"]
    summary_bits = [f"{r.get('action', '?')} {r.get('workload_slug', '?')}={r['status']}" for r in results]
    commit_msg = f"chore(lifecycle-receipt): {'; '.join(summary_bits)}"
    if not decommissioned:
        # Only a TTL change or rejections: nothing else must be triggered by this commit.
        commit_msg += " [skip ci]"
    pathlib.Path(args.commit_message_file).write_text(commit_msg + "\n", encoding="utf-8")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_path:
        with pathlib.Path(summary_path).open("a", encoding="utf-8") as fh:
            fh.write("## Override de ciclo de vida (aprovadores EA/PE)\n\n")
            fh.write("| Pedido | Workload | Ação | Solicitante | Resultado | Detalhe |\n|---|---|---|---|---|---|\n")
            for r in results:
                fh.write(
                    f"| `{r['request_file']}` | `{r.get('workload_slug', '')}` | {r.get('action', '')} | "
                    f"{r.get('requester_email', '')} | **{r['status']}** | {r.get('detail', '')} |\n"
                )
    if args.github_output:
        with pathlib.Path(args.github_output).open("a", encoding="utf-8") as fh:
            fh.write(f"processed_count={len(results)}\n")
            fh.write(f"applied_count={len(applied)}\n")
            fh.write(f"decommissioned_slugs={','.join(decommissioned)}\n")
    rejected = [r for r in results if r["status"] != "APPLIED"]
    if rejected:
        print(f"::warning::{len(rejected)} pedido(s) rejeitado(s); veja o resumo da execução.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
