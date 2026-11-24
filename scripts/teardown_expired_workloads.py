#!/usr/bin/env python3
"""Deterministic Workload-Only Teardown Executor (TTL Expiry & FinOps Hard Cap).

1. Detaches ONLY the expired workload's `.nip.io` hostRules & pathMatchers from `sdlc-shared-https-urlmap`
   (`reconcile_shared_gclb_routes.py remove`) so its BackendBucket & BackendServices are no longer in-use.
2. Detaches shared singleton resources (Shared GCLB Edge Plane + Shared SQL/Redis Singletons) from the
   workload's Terraform state (`terraform state rm`) so sibling apps stay online and `<ip>.nip.io` stays stable.
3. Runs `terraform destroy` to cleanly delete 100% of the expired workload's SPA GCS bucket, BackendBucket,
   Cloud Run v2 services, Serverless NEGs, BackendServices, Cloud Armor WAF, Certificate Manager certs & map
   entries, Cloud Run Functions v2, Pub/Sub topics/subscriptions, SLOs, Dashboards, and Alert Policies.
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import re
import shutil
import subprocess
import sys
import time
import tempfile

SHARED_SQL_ENGINES = {"postgresql", "mysql", "sqlserver"}


_SAFE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
DEFAULT_ARTIFACTS_BUCKET = "ernani-demo-argolis-sdlc-artifacts"


def cleanup_canvas_and_staging_artifacts(
    slug: str,
    project_id: str,
    artifacts_bucket: str = DEFAULT_ARTIFACTS_BUCKET,
) -> list[str]:
    """Deletes expired workload Canvas HTML, 4K architecture PNG, and `/ui-staging/<slug>/` from GCS.

    Best-effort & strictly scoped to exact validated `<slug>` paths so sibling workloads are never touched.
    """
    clean_slug = (slug or "").strip().lower()
    clean_bucket = (artifacts_bucket or "").strip().removeprefix("gs://").strip("/")
    if not clean_bucket or not _SAFE_SLUG_RE.match(clean_slug):
        return []
    if not shutil.which("gcloud"):
        return []

    targets: list[tuple[str, bool]] = [
        (f"gs://{clean_bucket}/canvas/{clean_slug}.html", False),
        (f"gs://{clean_bucket}/diagrams/{clean_slug}-4k.png", False),
        (f"gs://{clean_bucket}/ui-staging/{clean_slug}/", True),
    ]
    cleaned: list[str] = []
    for uri, recursive in targets:
        cmd = ["gcloud", "storage", "rm"]
        if recursive:
            cmd.append("--recursive")
        cmd.extend([uri, f"--project={project_id}", "--quiet"])
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
        stderr_l = (proc.stderr or "").lower()
        if proc.returncode == 0:
            cleaned.append(uri)
            print(f"[TEARDOWN] Artefato de Canvas/Staging removido: {uri}")
        elif "matched no objects" in stderr_l or "not found" in stderr_l or "404" in stderr_l:
            continue
        else:
            print(
                f"::warning::[TEARDOWN] Falha ao remover artefato '{uri}': "
                f"{(proc.stderr or proc.stdout or '').strip()[:240]}"
            )
    return cleaned


def app_database_name(slug: str) -> str:
    """Mirrors the generator naming (`db_<tf_id>`) used by `google_sql_database.app_db`."""
    tf_id = re.sub(r"[^a-z0-9_]", "_", slug.strip().lower())[:24].rstrip("_")
    return f"db_{tf_id}"

SHARED_SINGLETON_STATE_ADDRS: tuple[str, ...] = (
    # Shared SQL & Redis Singletons
    "google_sql_database_instance.shared_db[0]",
    "google_sql_database_instance.shared_db",
    "google_secret_manager_secret.shared_db_pass[0]",
    "google_secret_manager_secret.shared_db_pass",
    "google_secret_manager_secret_version.shared_db_pass_v1[0]",
    "google_secret_manager_secret_version.shared_db_pass_v1",
    "google_sql_user.shared_db_root[0]",
    "google_sql_user.shared_db_root",
    "random_password.shared_db_pass[0]",
    "random_password.shared_db_pass",
    "google_redis_instance.shared_redis[0]",
    "google_redis_instance.shared_redis",
    # Shared GCLB .nip.io Edge Plane & Optional Shared GKE Autopilot
    "google_compute_global_address.shared_gclb_ip[0]",
    "tls_private_key.shared_bootstrap_key[0]",
    "tls_self_signed_cert.shared_bootstrap_cert[0]",
    "google_certificate_manager_certificate.shared_bootstrap_cert[0]",
    "google_certificate_manager_certificate_map.shared_cert_map[0]",
    "google_certificate_manager_certificate_map_entry.shared_primary_cme[0]",
    "google_storage_bucket.shared_gclb_fallback[0]",
    "google_storage_bucket_object.shared_gclb_fallback_index[0]",
    "google_storage_bucket_iam_member.shared_gclb_fallback_public[0]",
    "google_compute_backend_bucket.shared_gclb_fallback[0]",
    "google_compute_url_map.shared_https_urlmap[0]",
    "google_compute_target_https_proxy.shared_https_proxy[0]",
    "google_compute_global_forwarding_rule.shared_https_fwd[0]",
    "google_compute_url_map.shared_http_redirect[0]",
    "google_compute_target_http_proxy.shared_http_proxy[0]",
    "google_compute_global_forwarding_rule.shared_http_fwd[0]",
    "google_container_cluster.shared_autopilot[0]",
)


def destroy_workload(
    lease_path: pathlib.Path,
    reason: str,
    destroy_shared_engines: bool = False,
) -> dict[str, str]:
    lease = json.loads(lease_path.read_text(encoding="utf-8"))
    slug = str(lease["workload_slug"])
    short_slug = str(lease.get("short_slug") or slug[:24].rstrip("-"))
    project_id = str(lease.get("target_gcp_project", "ernani-demo-argolis"))
    region = str(lease.get("region", "southamerica-east1"))
    if project_id != "ernani-demo-argolis":
        raise RuntimeError(f"Projeto inesperado '{project_id}' em {lease_path}")

    reconciler = lease_path.resolve().parents[1] / "scripts" / "reconcile_shared_gclb_routes.py"
    if reconciler.is_file() and not destroy_shared_engines:
        subprocess.run(
            [
                sys.executable,
                str(reconciler),
                "remove",
                f"--project={project_id}",
                f"--short-slug={short_slug}",
            ],
            check=False,
        )

    tf_bucket = "ernani-demo-argolis-sdlc-tfstate"
    with tempfile.TemporaryDirectory(prefix=f"teardown-{slug}-") as tmp:
        tmp_dir = pathlib.Path(tmp)
        bundle_path = tmp_dir / "tf-bundle.tar.gz"
        vars_path = tmp_dir / "vars.json"
        extract_root = tmp_dir / "bundle"
        extract_root.mkdir(parents=True, exist_ok=True)

        subprocess.run(
            [
                "gcloud",
                "storage",
                "cp",
                f"gs://{tf_bucket}/workloads/{slug}/tf-bundle.tar.gz",
                str(bundle_path),
                f"--project={project_id}",
                "--quiet",
            ],
            check=True,
        )
        subprocess.run(
            [
                "gcloud",
                "storage",
                "cp",
                f"gs://{tf_bucket}/workloads/{slug}/vars.json",
                str(vars_path),
                f"--project={project_id}",
                "--quiet",
            ],
            check=True,
        )
        subprocess.run(["tar", "-xzf", str(bundle_path), "-C", str(extract_root)], check=True)
        tf_dir = (
            extract_root / "infra" / "terraform"
            if (extract_root / "infra" / "terraform").is_dir()
            else extract_root
        )

        subprocess.run(
            [
                "terraform",
                f"-chdir={tf_dir}",
                "init",
                f"-backend-config=bucket={tf_bucket}",
                f"-backend-config=prefix=workloads/{slug}",
                "-input=false",
            ],
            check=True,
        )

        if not destroy_shared_engines:
            # Ensure shared_gclb_already_exists and shared_db_already_exists are true during destroy
            vdata = json.loads(vars_path.read_text(encoding="utf-8"))
            vdata["shared_gclb_already_exists"] = True
            vdata["shared_db_already_exists"] = True
            vars_path.write_text(json.dumps(vdata, indent=2) + "\n", encoding="utf-8")

            for addr in SHARED_SINGLETON_STATE_ADDRS:
                subprocess.run(
                    ["terraform", f"-chdir={tf_dir}", "state", "rm", addr],
                    check=False,
                    capture_output=True,
                )

        subprocess.run(
            [
                "terraform",
                f"-chdir={tf_dir}",
                "destroy",
                "-auto-approve",
                "-input=false",
                f"-var-file={vars_path}",
            ],
            check=True,
        )

    sched_job = f"ttl-exp-{slug}"
    db_type = str(lease.get("database_type", "")).strip().lower()
    shared_sql = str(lease.get("shared_sql_instance", "")).strip()
    if (
        not destroy_shared_engines
        and db_type in SHARED_SQL_ENGINES
        and shared_sql
        and shutil.which("gcloud")
    ):
        # google_sql_database.app_db usa deletion_policy=ABANDON (o destroy nao derruba conexoes ativas);
        # com os servicos da app ja destruidos, removemos o database logico para que um novo deploy
        # do mesmo workload nao falhe com "database already exists".
        app_db = app_database_name(slug)
        db_proc = None
        for attempt in range(1, 5):
            db_proc = subprocess.run(
                [
                    "gcloud",
                    "sql",
                    "databases",
                    "delete",
                    app_db,
                    f"--instance={shared_sql}",
                    f"--project={project_id}",
                    "--quiet",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if db_proc.returncode == 0 or "not found" in (db_proc.stderr or "").lower():
                break
            if attempt < 4 and "being accessed by other users" in (db_proc.stderr or "").lower():
                print(f"[TEARDOWN] Aguardando conexões residuais de '{app_db}' encerrarem (tentativa {attempt}/4)...")
                time.sleep(10)
            else:
                break
        if db_proc.returncode == 0:
            print(f"[TEARDOWN] Database lógico '{app_db}' removido do singleton '{shared_sql}'.")
        else:
            print(
                f"::warning::[TEARDOWN] Database lógico '{app_db}' não removido de '{shared_sql}': "
                f"{(db_proc.stderr or db_proc.stdout or '').strip()[:240]}"
            )
    if shutil.which("gcloud"):
        subprocess.run(
            [
                "gcloud",
                "scheduler",
                "jobs",
                "delete",
                sched_job,
                f"--location={region}",
                f"--project={project_id}",
                "--quiet",
            ],
            check=False,
            capture_output=True,
        )

    artifacts_bucket = str(lease.get("artifacts_bucket") or DEFAULT_ARTIFACTS_BUCKET).strip()
    artifacts_cleaned = cleanup_canvas_and_staging_artifacts(
        slug=slug,
        project_id=project_id,
        artifacts_bucket=artifacts_bucket,
    )

    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lease["status"] = "DECOMMISSIONED"
    if artifacts_cleaned:
        lease["artifacts_cleaned"] = artifacts_cleaned
    lease["decommissioned_at_utc"] = now_utc
    lease["decommissioned_reason"] = reason
    lease_path.write_text(json.dumps(lease, indent=2) + "\n", encoding="utf-8")
    return {
        "workload_slug": slug,
        "decommissioned_at_utc": now_utc,
        "reason": reason,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Teardown expired or budget-capped sandbox workloads.")
    parser.add_argument("--workload-slug", default="", help="Optional target workload slug")
    parser.add_argument("--reason", default="TTL_EXPIRED", help="Audit reason for teardown")
    parser.add_argument("--force-now", default="false", help="Destroy immediately even before expires_at_epoch")
    parser.add_argument("--destroy-shared-engines", default="false", help="Also destroy shared singleton GCLB/DB/Redis")
    parser.add_argument("--github-output", default="", help="Optional $GITHUB_OUTPUT path")
    args = parser.parse_args()

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    leases_dir = repo_root / "active-leases"
    if not leases_dir.is_dir():
        print("Nenhum diretório active-leases encontrado; nada a descomissionar.")
        return 0

    now_epoch = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    force_now = str(args.force_now).strip().lower() == "true"
    destroy_shared = str(args.destroy_shared_engines).strip().lower() == "true"
    target_slug = args.workload_slug.strip()

    destroyed: list[dict[str, str]] = []
    for lease_file in sorted(leases_dir.glob("*.json")):
        data = json.loads(lease_file.read_text(encoding="utf-8"))
        if str(data.get("status", "")).upper() != "ACTIVE":
            continue
        slug = str(data.get("workload_slug", ""))
        if target_slug and slug != target_slug:
            continue

        expires_epoch = int(data.get("expires_at_epoch", 0))
        simulated_spend = float(data.get("simulated_spend_brl", 0.0) or 0.0)
        budget_brl = float(data.get("budget_brl", 0.0) or 0.0)

        should_destroy = False
        effective_reason = args.reason
        if force_now and target_slug == slug:
            should_destroy = True
        elif budget_brl > 0 and simulated_spend >= budget_brl:
            should_destroy = True
            effective_reason = f"BUDGET_HARD_CAP_REACHED ({simulated_spend:.2f}/{budget_brl:.2f} BRL)"
        elif expires_epoch > 0 and now_epoch >= expires_epoch:
            should_destroy = True
            requested_by = str(data.get("decommission_requested_by", "")).strip()
            if requested_by:
                effective_reason = f"DECOMMISSION_REQUESTED ({requested_by})"
            else:
                effective_reason = args.reason or f"TTL_EXPIRED ({data.get('ttl', '1h')})"

        if should_destroy:
            print(
                f"[TEARDOWN] Executando terraform destroy exclusivo dos workloads de '{slug}' "
                f"(shared_singletons_preserved={not destroy_shared}, reason={effective_reason})..."
            )
            rec = destroy_workload(
                lease_file,
                effective_reason,
                destroy_shared_engines=destroy_shared,
            )
            destroyed.append(rec)

    if args.github_output:
        with pathlib.Path(args.github_output).open("a", encoding="utf-8") as fh:
            fh.write(f"destroyed_count={len(destroyed)}\n")
            fh.write(
                "destroyed_slugs="
                + ",".join(item["workload_slug"] for item in destroyed)
                + "\n"
            )

    print(f"Total de workloads descomissionados nesta execução: {len(destroyed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
