#!/usr/bin/env python3
"""FinOps Idle Reaper da infraestrutura compartilhada (Cloud SQL, Memorystore Redis e GCLB `sdlc-shared-*`).

Problema resolvido: o teardown (`teardown_expired_workloads.py`) preserva de propósito os singletons
compartilhados (`terraform state rm` + drop do database lógico `db_<slug>`), então, depois que o último
workload saía, o servidor de banco continuava ligado e faturando vCPU/RAM 24x7 sem nenhum uso.

Política de ociosidade (projeto `ernani-demo-argolis`):
  * Cloud SQL (`sdlc-shared-pgsql`, `sdlc-shared-mysql`, `sdlc-shared-sqlserver`): quando ocioso o
    servidor é PARADO (`gcloud sql instances patch <nome> --activation-policy=NEVER`). Parado não cobra
    vCPU/RAM (restam só disco e IP), mantém configuração, usuários e segredos e volta em poucos minutos.
    Instâncias Cloud SQL NUNCA são apagadas: o nome de uma instância apagada fica reservado por até ~7 dias
    e o próximo deploy não conseguiria recriar `sdlc-shared-*` (quebraria o provisionamento).
  * Memorystore Redis (`sdlc-shared-redis`): não pode ser parado; quando ocioso é APAGADO e o Terraform o
    recria sob demanda no próximo deploy que pedir Redis. Como a detecção de uso de Redis por lease é
    indireta, o Redis só é apagado quando NÃO há nenhum lease em uso (de qualquer engine).
  * GCLB compartilhado (plano de borda `sdlc-shared-*`: 2 forwarding rules, 2 proxies, 2 URL maps, backend
    bucket + bucket de fallback, IP global estático, Certificate Map + entrada primária e certificado bootstrap
    = 12 recursos): quando NENHUM workload existe (0 leases em uso, 0 provisionamentos em andamento e 0
    `hostRules` em `sdlc-shared-https-urlmap`), é REMOVIDO por completo em ordem reversa de dependência,
    de forma idempotente (recurso já ausente = segue), sem forwarding rule nem IP estático cobrando à toa.
    O próximo deploy recria os 12 recursos (`reconcile_shared_state.sh` -> `shared_gclb_already_exists=false`),
    com um IP novo (os hosts `<ip>.nip.io` mudam).
  * Religamento automático: `scripts/provision_workload.sh` executa `reap_idle_shared_backing.py resume`
    antes do Terraform (Cloud SQL parado -> `--activation-policy=ALWAYS` + espera RUNNABLE; Redis ainda em
    remoção -> espera terminar; GCLB com remoção incompleta -> conclui a remoção para o Terraform recriar
    os 12 recursos sem conflito 409).

Um servidor só é considerado OCIOSO quando TODAS as verificações passam. Qualquer dúvida (API do GitHub
ou do GCP indisponível, lease ilegível ou sem engine identificável, operação pendente, rótulo ausente...)
significa NÃO fazer nada (fail-safe; código de saída 3 para a falha ficar visível). Imediatamente antes de
cada alteração a avaliação COMPLETA é refeita do zero (leases, runs, inventário Cloud Run, databases).
Verificações por servidor:
  1. rótulo `managed_by=cloud-provisioning-approvals` (instâncias não gerenciadas nunca são tocadas);
  2. sem opt-out (`finops_idle_reaper=disabled` mantém o servidor ligado);
  3. `deletionProtectionEnabled=false` (proteção contra deleção é respeitada: pula + aviso);
  4. em execução (Cloud SQL `RUNNABLE` + `ALWAYS`; Redis `READY`);
  5. sem operações Cloud SQL pendentes na instância;
  6. nenhum lease `ACTIVE`/`PROVISIONING` (ou não `DECOMMISSIONED` e não expirado) usando o engine
     (checkout local + `origin/main` via `git fetch`; Redis: nenhum lease em uso de qualquer engine);
  7. nenhuma execução de `provision-approved-workload.yml` em `queued`/`in_progress`/`waiting`/
     `requested`/`pending` (API do GitHub);
  8. somente databases de sistema na instância (Cloud SQL);
  9. nenhum serviço/job Cloud Run (southamerica-east1, us-central1) referenciando a instância
     (connection name, anotação `run.googleapis.com/cloudsql-instances`, volumes Cloud SQL, IP, nome, segredo);
 10. último descomissionamento de workload do engine há mais de `--grace-minutes` (o workflow usa 0: o
     último teardown já para/remove tudo na mesma execução; o cron horário é a rede de segurança).
GCLB: 0 leases em uso (qualquer engine), 0 provisionamentos em andamento, 0 `hostRules` no URL map HTTPS,
IP global usado só pelas forwarding rules do próprio GCLB, sem opt-out e carência cumprida.

O repositório é público: o log nunca lista IPs, nomes de serviços fora da esteira ou e-mails.

Uso:
  python3 scripts/reap_idle_shared_backing.py --project ernani-demo-argolis --dry-run true
  python3 scripts/reap_idle_shared_backing.py --project ernani-demo-argolis --dry-run false --grace-minutes 0 \\
      --github-output "$GITHUB_OUTPUT"
  python3 scripts/reap_idle_shared_backing.py resume --project ernani-demo-argolis --engine postgresql \\
      --enable-redis false --phase wait
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Sequence

DEFAULT_PROJECT = "ernani-demo-argolis"
DEFAULT_REGION = "southamerica-east1"
DEFAULT_RUN_REGIONS: tuple[str, ...] = ("southamerica-east1", "us-central1")
DEFAULT_REPO = "gemini-enterprise-sdlc-org/cloud-provisioning-approvals"
DEFAULT_GRACE_MINUTES = 10
PIPELINE_SERVICE_ACCOUNT = "sdlc-provisioner-sa@ernani-demo-argolis.iam.gserviceaccount.com"
PROVISION_WORKFLOW = "provision-approved-workload.yml"
MANAGED_BY_VALUE = "cloud-provisioning-approvals"
OPT_OUT_LABEL = "finops_idle_reaper"
OPT_OUT_VALUES = frozenset({"disabled", "off", "false", "skip", "keep", "never"})
IN_FLIGHT_RUN_STATUSES: tuple[str, ...] = ("queued", "in_progress", "waiting", "requested", "pending")
LEASE_IN_USE_STATUSES = frozenset({"ACTIVE", "PROVISIONING"})
STATELESS_DATABASE_TYPES = frozenset({"none", "stateless"})
GCLB_ENGINE = "gclb"
GCLB_LOGICAL_NAME = "sdlc-shared-gclb"
# Nomes fixos gerados por `app/tools/gclb_spa_and_serverless_iac.py` (plataforma). Nunca usar curingas.
GCLB_NAMES: dict[str, str] = {
    "https_fwd": "sdlc-shared-https-fwd",
    "http_fwd": "sdlc-shared-http-fwd",
    "https_proxy": "sdlc-shared-https-proxy",
    "http_proxy": "sdlc-shared-http-proxy",
    "https_urlmap": "sdlc-shared-https-urlmap",
    "http_redirect": "sdlc-shared-http-redirect",
    "fallback_backend": "sdlc-shared-gclb-fallback-be",
    "ip": "sdlc-shared-gclb-ip",
    "cert_map": "sdlc-shared-cert-map",
    "primary_cme": "sdlc-shared-primary-cme",
    "bootstrap_cert": "sdlc-shared-bootstrap-cert",
}
GCLB_FALLBACK_BUCKET_PREFIX = "sdlc-shared-gclb-fb-"
GCLB_DEFAULT_FALLBACK_BUCKET = "sdlc-shared-gclb-fb-270170641495"
GCLB_TOTAL_RESOURCES = 12  # 11 nomes acima (sem o bucket) + o bucket de fallback
GCLB_DELETE_ATTEMPTS = 3
REDIS_TRANSITIONAL_STATES = frozenset({"CREATING", "UPDATING", "DELETING", "REPAIRING", "MAINTENANCE", "IMPORTING", "FAILING_OVER"})
EXIT_OK, EXIT_ACTION_FAILED, EXIT_USAGE, EXIT_FAIL_SAFE = 0, 1, 2, 3

SQL_SINGLETONS: dict[str, dict[str, Any]] = {
    "postgresql": {
        "instance": "sdlc-shared-pgsql",
        "secret_id": "sdlc-shared-pgsql-root-pass",
        "system_databases": frozenset({"postgres", "cloudsqladmin", "template0", "template1"}),
    },
    "mysql": {
        "instance": "sdlc-shared-mysql",
        "secret_id": "sdlc-shared-mysql-root-pass",
        "system_databases": frozenset({"mysql", "information_schema", "performance_schema", "sys"}),
    },
    "sqlserver": {
        "instance": "sdlc-shared-sqlserver",
        "secret_id": "sdlc-shared-sqlserver-root-pass",
        "system_databases": frozenset({"master", "model", "msdb", "tempdb"}),
    },
}
REDIS_ENGINE = "redis"
REDIS_INSTANCE = "sdlc-shared-redis"
REDIS_SECRET_ID = "sdlc-shared-redis-auth"
ALL_ENGINES: frozenset[str] = frozenset({*SQL_SINGLETONS, REDIS_ENGINE})

ENGINE_ALIASES: dict[str, str] = {
    # 'auto' segue a convenção do gerador da plataforma (auto -> postgresql): conservador no reaper
    # (conta como uso do pgsql) e correto no religamento.
    "auto": "postgresql",
    "pg": "postgresql",
    "pgsql": "postgresql",
    "postgres": "postgresql",
    "postgresql": "postgresql",
    "mysql": "mysql",
    "mariadb": "mysql",
    "mssql": "sqlserver",
    "sqlserver": "sqlserver",
    "sql-server": "sqlserver",
    "sql_server": "sqlserver",
}

# Somente formas conhecidas de "recurso inexistente" (erros genéricos como "file ... does not exist" de
# credenciais NÃO contam como ausência): Cloud SQL `HTTPError 404: The Cloud SQL instance does not exist.`,
# Memorystore `NOT_FOUND: Resource '...' was not found`, Compute `The resource '...' was not found`.
NOT_FOUND_MARKERS: tuple[str, ...] = (
    "httperror 404",
    "not_found",
    "the cloud sql instance does not exist",
    "' was not found",
    "not found: 404",  # Cloud Storage: `gs://<bucket> not found: 404.`
    "matched no objects or files",  # `gcloud storage rm` em bucket já removido
)

PIPELINE_DB_RE = re.compile(r"^db_[a-z0-9_]{1,40}$")

CHECK_LABELS: dict[str, str] = {
    "managed_label": "rótulo managed_by=cloud-provisioning-approvals (instância gerenciada pela esteira)",
    "opt_out_label": f"sem opt-out (rótulo {OPT_OUT_LABEL} != disabled)",
    "deletion_protection": "deletionProtectionEnabled=false (proteção contra deleção respeitada)",
    "running_state": "servidor em execução",
    "pending_operations": "sem operações Cloud SQL pendentes na instância",
    "active_leases": "nenhum lease ACTIVE/PROVISIONING (ou não expirado) usando o engine",
    "in_flight_provisioning": f"nenhum {PROVISION_WORKFLOW} em andamento",
    "application_databases": "somente databases de sistema (nenhum database de aplicação)",
    "cloud_run_references": "nenhum serviço/job Cloud Run referenciando o servidor",
    "grace_period": "último descomissionamento do engine fora da carência",
    "url_map_host_rules": "0 hostRules de workloads em sdlc-shared-https-urlmap",
    "address_users": "IP global usado só pelas forwarding rules do GCLB compartilhado",
    "cert_map_entries": "Certificate Map só com a entrada bootstrap (órfãs de workloads descomissionados são removidas)",
}

DECISION_LABELS: dict[str, str] = {
    "absent": "INEXISTENTE (nada a fazer)",
    "already_stopped": "JÁ PARADO (activation-policy NEVER)",
    "in_use": "MANTIDO LIGADO (em uso ou protegido)",
    "would_stop": "SERIA PARADO (activation-policy NEVER)",
    "stopped": "PARADO (activation-policy NEVER)",
    "would_delete": "SERIA APAGADO (Memorystore Redis)",
    "deleted": "APAGADO (Memorystore Redis)",
    "skipped_race": "MANTIDO LIGADO (estado mudou na revalidação)",
    "error": "ERRO (nenhuma ação)",
}

GCLB_DECISION_LABELS: dict[str, str] = {
    "would_delete": "SERIA APAGADO (todos os recursos do GCLB compartilhado, 0 hostRules)",
    "deleted": "APAGADO (todos os recursos do GCLB compartilhado)",
    "in_use": "MANTIDO (em uso ou protegido)",
}


def decision_label(kind: str, decision: str) -> str:
    if kind == "shared-gclb" and decision in GCLB_DECISION_LABELS:
        return GCLB_DECISION_LABELS[decision]
    return DECISION_LABELS.get(decision, decision)


class ExternalQueryError(RuntimeError):
    """Falha ao consultar ou alterar GCP/GitHub/git. O reaper trata como fail-safe (nenhuma ação)."""


@dataclasses.dataclass
class CmdResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[list[str]], CmdResult]


def subprocess_runner(argv: list[str], timeout: int = 1200) -> CmdResult:
    """Executa um comando externo sem shell, capturando stdout/stderr."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        return CmdResult(127, "", f"executável não encontrado: {exc}")
    except subprocess.TimeoutExpired:
        return CmdResult(124, "", f"timeout de {timeout}s em: {' '.join(argv[:5])}")
    return CmdResult(proc.returncode, proc.stdout or "", proc.stderr or "")


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def _safe(text: Any, limit: int = 200) -> str:
    """Texto externo seguro para log público: sem quebras/controle (evita injetar `::comandos::`)."""
    clean = _CONTROL_RE.sub(" ", str(text or ""))
    clean = " ".join(clean.split()).replace("::", ": :")
    return clean if len(clean) <= limit else clean[: limit - 3] + "..."


def _is_not_found(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in NOT_FOUND_MARKERS)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "sim", "on"}


def _to_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _parse_utc(value: Any) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _utc_iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_engine(raw: Any) -> str:
    """Normaliza aliases de `database_type` para o engine singleton canônico ('' se não houver)."""
    return ENGINE_ALIASES.get(str(raw or "").strip().lower(), "")


def _names_instance(value: Any, instance: str) -> bool:
    """`sdlc-shared-x`, `projects/.../instances/sdlc-shared-x` ou `proj:region:sdlc-shared-x`."""
    raw = str(value or "").strip()
    return bool(raw) and (raw == instance or raw.endswith("/" + instance) or raw.endswith(":" + instance))


def str_to_bool(raw: str) -> bool:
    value = str(raw).strip().lower()
    if value in {"true", "1", "yes", "sim"}:
        return True
    if value in {"false", "0", "no", "nao", "não"}:
        return False
    raise argparse.ArgumentTypeError(f"valor booleano inválido: '{raw}' (use true|false)")


# ---------------------------------------------------------------------------------------------
# Clientes externos (gcloud / gh / git). Todos passam por um Runner injetável para permitir testes.
# ---------------------------------------------------------------------------------------------


class Gcp:
    """Wrapper mínimo do gcloud usado pelo reaper e pelo religamento automático."""

    def __init__(self, project: str, runner: Runner = subprocess_runner, gcloud: str = "gcloud") -> None:
        self.project = project
        self.runner = runner
        self.gcloud = gcloud

    def _run(self, args: Sequence[str]) -> CmdResult:
        return self.runner([self.gcloud, *args, f"--project={self.project}", "--format=json"])

    def _json(self, args: Sequence[str], *, allow_not_found: bool = False) -> Any:
        res = self._run(args)
        if res.returncode != 0:
            err = (res.stderr or res.stdout or "").strip()
            if allow_not_found and _is_not_found(err):
                return None
            raise ExternalQueryError(f"gcloud {' '.join(args[:3])} falhou (rc={res.returncode}): {_safe(err)}")
        text = (res.stdout or "").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ExternalQueryError(f"gcloud {' '.join(args[:3])} devolveu JSON inválido: {exc}") from exc

    # ---- leitura -------------------------------------------------------------------------
    def describe_sql(self, name: str) -> dict[str, Any] | None:
        data = self._json(["sql", "instances", "describe", name], allow_not_found=True)
        return data if isinstance(data, dict) else None

    def list_sql_databases(self, name: str) -> list[str]:
        data = self._json(["sql", "databases", "list", f"--instance={name}"]) or []
        return [str(item.get("name", "")) for item in data if isinstance(item, dict)]

    def list_sql_operations(self, name: str) -> list[dict[str, Any]]:
        data = self._json(["sql", "operations", "list", f"--instance={name}", "--limit=25"]) or []
        return [op for op in data if isinstance(op, dict)]

    def list_pending_sql_operations(self, name: str) -> list[dict[str, Any]]:
        return [op for op in self.list_sql_operations(name) if str(op.get("status", "")).upper() not in {"DONE", ""}]

    def describe_redis(self, name: str, region: str) -> dict[str, Any] | None:
        data = self._json(["redis", "instances", "describe", name, f"--region={region}"], allow_not_found=True)
        return data if isinstance(data, dict) else None

    def list_run_resources(self, kind: str, region: str) -> list[dict[str, Any]]:
        data = self._json(["run", kind, "list", f"--region={region}"]) or []
        return [item for item in data if isinstance(item, dict)]

    def describe_any(self, args: Sequence[str]) -> dict[str, Any] | None:
        data = self._json(args, allow_not_found=True)
        return data if isinstance(data, dict) else None

    def list_cert_map_entries(self, cert_map: str) -> list[dict[str, Any]] | None:
        res = self._run(["certificate-manager", "maps", "entries", "list", f"--map={cert_map}", "--location=global"])
        if res.returncode != 0:
            err = (res.stderr or res.stdout or "").strip()
            if _is_not_found(err):
                return None
            raise ExternalQueryError(f"gcloud certificate-manager maps entries list falhou (rc={res.returncode}): {_safe(err)}")
        try:
            data = json.loads((res.stdout or "").strip() or "[]")
        except json.JSONDecodeError as exc:
            raise ExternalQueryError(f"entries list devolveu JSON inválido: {exc}") from exc
        return [e for e in data if isinstance(e, dict)]

    # ---- alteração (nunca chamadas em dry-run) ---------------------------------------------
    def _mutate(self, args: Sequence[str]) -> str:
        """Executa um comando que altera recursos; devolve o nome da operação (ou '' se não informado).

        Um stdout fora do formato esperado NÃO é tratado como falha: o comando já foi aceito (rc=0).
        """
        res = self._run(args)
        if res.returncode != 0:
            raise ExternalQueryError(f"gcloud {' '.join(args[:3])} falhou (rc={res.returncode}): {_safe(res.stderr or res.stdout)}")
        try:
            data = json.loads((res.stdout or "").strip() or "null")
        except json.JSONDecodeError:
            return ""
        if isinstance(data, list) and data:
            data = data[0]
        return str(data.get("name", "")) if isinstance(data, dict) else ""

    def set_sql_activation_policy(self, name: str, policy: str) -> str:
        return self._mutate(["sql", "instances", "patch", name, f"--activation-policy={policy}", "--async", "--quiet"])

    def wait_sql_operation(self, operation: str, timeout_s: int) -> None:
        res = self._run(["sql", "operations", "wait", operation, f"--timeout={int(timeout_s)}"])
        if res.returncode != 0:
            raise ExternalQueryError(f"operação Cloud SQL {operation} não concluída: {_safe(res.stderr or res.stdout)}")

    def delete_redis(self, name: str, region: str) -> str:
        return self._mutate(["redis", "instances", "delete", name, f"--region={region}", "--async", "--quiet"])

    def delete_any(self, args: Sequence[str]) -> bool:
        """Apaga um recurso (síncrono). True = removido agora; False = já estava ausente (idempotente)."""
        res = self._run(args)
        if res.returncode == 0:
            return True
        err = (res.stderr or res.stdout or "").strip()
        if _is_not_found(err):
            return False
        raise ExternalQueryError(f"gcloud {' '.join(args[:3])} falhou (rc={res.returncode}): {_safe(err)}")


class GitHub:
    """Consulta somente-leitura de runs via `gh api` (usa GH_TOKEN/GITHUB_TOKEN ou `gh auth`)."""

    def __init__(self, repo: str, runner: Runner = subprocess_runner, gh: str = "gh") -> None:
        self.repo = repo
        self.runner = runner
        self.gh = gh

    def in_flight_runs(self, workflow: str = PROVISION_WORKFLOW) -> list[dict[str, Any]]:
        found: dict[Any, dict[str, Any]] = {}
        for status in IN_FLIGHT_RUN_STATUSES:
            path = f"repos/{self.repo}/actions/workflows/{workflow}/runs?status={status}&per_page=20"
            res = self.runner([self.gh, "api", "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28", path])
            if res.returncode != 0:
                raise ExternalQueryError(f"GitHub API (runs {status}) falhou (rc={res.returncode}): {_safe(res.stderr or res.stdout)}")
            try:
                data = json.loads(res.stdout or "null")
            except json.JSONDecodeError as exc:
                raise ExternalQueryError(f"GitHub API (runs {status}) devolveu JSON inválido: {exc}") from exc
            if not isinstance(data, dict) or not isinstance(data.get("workflow_runs"), list):
                raise ExternalQueryError(f"resposta inesperada da API de runs ({status})")
            for run in data["workflow_runs"]:
                found[run.get("id")] = {
                    "id": run.get("id"),
                    "status": run.get("status"),
                    "event": run.get("event"),
                    "created_at": run.get("created_at"),
                    "html_url": run.get("html_url"),
                }
        return list(found.values())


class GitLeases:
    """Lê `active-leases/*.json` do `origin/main` via `git fetch` (sem consumir a cota da API do GitHub)."""

    REF = "refs/remotes/origin/main"

    def __init__(self, repo_dir: pathlib.Path, runner: Runner = subprocess_runner, git: str = "git") -> None:
        self.repo_dir = repo_dir
        self.runner = runner
        self.git = git

    def _git(self, *args: str) -> CmdResult:
        return self.runner([self.git, "-C", str(self.repo_dir), *args])

    def fetch_remote_leases(self) -> dict[str, str]:
        res = self._git("fetch", "--quiet", "--no-tags", "origin", f"+refs/heads/main:{self.REF}")
        if res.returncode != 0:
            raise ExternalQueryError(f"git fetch origin main falhou (rc={res.returncode}): {_safe(res.stderr or res.stdout)}")
        res = self._git("ls-tree", "--name-only", self.REF, "active-leases/")
        if res.returncode != 0:
            raise ExternalQueryError(f"git ls-tree active-leases/ falhou: {_safe(res.stderr or res.stdout)}")
        leases: dict[str, str] = {}
        for path in (res.stdout or "").splitlines():
            path = path.strip()
            if not path.endswith(".json"):
                continue
            show = self._git("show", f"{self.REF}:{path}")
            if show.returncode != 0:
                raise ExternalQueryError(f"git show {path} falhou: {_safe(show.stderr or show.stdout)}")
            leases[pathlib.PurePosixPath(path).name] = show.stdout
        return leases


# ---------------------------------------------------------------------------------------------
# Leases
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass
class LeaseView:
    source: str
    slug: str
    status: str
    engines: frozenset[str]
    in_use: bool
    reason: str
    decommissioned_at: dt.datetime | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "workload_slug": self.slug,
            "status": self.status,
            "engines": sorted(self.engines),
            "in_use": self.in_use,
            "reason": self.reason,
            "decommissioned_at_utc": self.decommissioned_at.strftime("%Y-%m-%dT%H:%M:%SZ") if self.decommissioned_at else None,
        }


def lease_engines(lease: dict[str, Any]) -> frozenset[str]:
    """Engines singleton usados por um lease (`database_type`, `shared_sql_instance`, Redis).

    Lease sem engine identificável e sem `database_type` explicitamente stateless ('none') conta como
    usuário de TODOS os engines (fail-safe).
    """
    engines: set[str] = set()
    raw_db_type = str(lease.get("database_type") or "").strip().lower()
    engine = normalize_engine(raw_db_type)
    if engine:
        engines.add(engine)
    for name, spec in SQL_SINGLETONS.items():
        if _names_instance(lease.get("shared_sql_instance"), spec["instance"]):
            engines.add(name)
    if _names_instance(lease.get("shared_redis_instance"), REDIS_INSTANCE) or _truthy(lease.get("enable_redis")):
        engines.add(REDIS_ENGINE)
    if not engines and raw_db_type not in STATELESS_DATABASE_TYPES:
        return ALL_ENGINES
    return frozenset(engines)


def evaluate_lease(source: str, raw_text: str, now_epoch: int) -> LeaseView:
    """Classifica um lease. JSON ilegível bloqueia todos os engines (fail-safe)."""
    try:
        data = json.loads(raw_text)
        if not isinstance(data, dict):
            raise ValueError("lease não é um objeto JSON")
    except (ValueError, TypeError) as exc:
        return LeaseView(
            source=source,
            slug="?",
            status="ILEGIVEL",
            engines=ALL_ENGINES,
            in_use=True,
            reason=f"lease ilegível ({_safe(exc, 80)}) — fail-safe bloqueia todos os engines",
        )

    slug = _safe(data.get("workload_slug") or pathlib.PurePosixPath(source).stem, 60)
    status = str(data.get("status") or "").strip().upper()
    expires_epoch = _to_int(data.get("expires_at_epoch"))
    if status in LEASE_IN_USE_STATUSES:
        in_use, reason = True, f"status={status}"
    elif status == "DECOMMISSIONED":
        in_use, reason = False, "DECOMMISSIONED"
    elif expires_epoch <= 0 or now_epoch < expires_epoch:
        in_use, reason = True, f"status={_safe(status, 20) or 'VAZIO'} ainda não expirado"
    else:
        in_use, reason = False, f"status={_safe(status, 20) or 'VAZIO'} expirado em {_utc_iso(expires_epoch)}"
    engines = lease_engines(data)
    if in_use and engines == ALL_ENGINES and not normalize_engine(data.get("database_type")):
        reason += " (engine não identificado no lease — fail-safe: todos os engines)"
    return LeaseView(
        source=source,
        slug=slug,
        status=_safe(status, 20) or "VAZIO",
        engines=engines,
        in_use=in_use,
        reason=reason,
        decommissioned_at=_parse_utc(data.get("decommissioned_at_utc")),
    )


def load_local_leases(leases_dir: pathlib.Path | None, now_epoch: int) -> list[LeaseView]:
    if leases_dir is None or not leases_dir.is_dir():
        return []
    views: list[LeaseView] = []
    for path in sorted(leases_dir.glob("*.json")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            text = f"<<erro de leitura: {exc}>>"
        views.append(evaluate_lease(f"local:{path.name}", text, now_epoch))
    return views


# ---------------------------------------------------------------------------------------------
# Avaliação por servidor
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass
class Check:
    check_id: str
    ok: bool
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {"check": self.check_id, "label": CHECK_LABELS.get(self.check_id, self.check_id), "ok": self.ok, "detail": self.detail}


@dataclasses.dataclass
class Evaluation:
    engine: str
    kind: str
    instance: str
    exists: bool = False
    state: str = ""
    activation_policy: str = ""
    tier: str = ""
    checks: list[Check] = dataclasses.field(default_factory=list)
    decision: str = "error"
    action: str = ""
    detail: str = ""
    operation: str = ""
    warnings: list[str] = dataclasses.field(default_factory=list)
    extra: dict[str, Any] = dataclasses.field(default_factory=dict)  # não serializado (ex.: inventário do GCLB)

    @property
    def blocking(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def to_json(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "kind": self.kind,
            "instance": self.instance,
            "exists": self.exists,
            "state": self.state,
            "activation_policy": self.activation_policy,
            "tier": self.tier,
            "decision": self.decision,
            "decision_label": decision_label(self.kind, self.decision),
            "action": self.action,
            "detail": self.detail,
            "operation": self.operation,
            "checks": [c.to_json() for c in self.checks],
            "warnings": self.warnings,
        }


@dataclasses.dataclass
class SharedContext:
    """Visões globais (leases, runs, inventário Cloud Run) calculadas a cada (re)avaliação."""

    leases: list[LeaseView]
    leases_error: str
    in_flight: list[dict[str, Any]]
    in_flight_error: str
    run_inventory: list[dict[str, Any]]
    run_inventory_error: str
    now_epoch: int
    grace_minutes: int

    @property
    def lease_slugs(self) -> set[str]:
        return {lv.slug for lv in self.leases if lv.slug and lv.slug != "?"}


def _check_leases(ctx: SharedContext, engine: str) -> Check:
    if ctx.leases_error:
        return Check("active_leases", False, f"não foi possível ler os leases ({ctx.leases_error}) — fail-safe")
    if engine == REDIS_ENGINE:
        # Uso de Redis por lease é indireto (campos opcionais): só apaga sem NENHUM lease em uso.
        users = [lv for lv in ctx.leases if lv.in_use]
        rule = " (Redis só é apagado sem nenhum lease em uso de qualquer engine)"
    elif engine == GCLB_ENGINE:
        users = [lv for lv in ctx.leases if lv.in_use]
        rule = " (GCLB compartilhado só é removido sem nenhum workload em uso)"
    else:
        users = [lv for lv in ctx.leases if lv.in_use and engine in lv.engines]
        rule = ""
    if users:
        names = ", ".join(sorted({f"{lv.slug} [{lv.reason}; {lv.source}]" for lv in users}))
        return Check("active_leases", False, f"em uso por: {names}{rule}")
    considered = [lv for lv in ctx.leases if engine in lv.engines] if engine != GCLB_ENGINE else list(ctx.leases)
    return Check(
        "active_leases",
        True,
        f"{len(considered)} lease(s) do engine, todos DECOMMISSIONED/expirados ({len(ctx.leases)} lidos no total){rule}",
    )


def _check_in_flight(ctx: SharedContext) -> Check:
    if ctx.in_flight_error:
        return Check("in_flight_provisioning", False, f"API do GitHub indisponível ({ctx.in_flight_error}) — fail-safe: nenhuma ação")
    if ctx.in_flight:
        runs = ", ".join(f"#{r.get('id')} ({r.get('status')})" for r in ctx.in_flight)
        return Check("in_flight_provisioning", False, f"provisionamento em andamento: {runs}")
    return Check("in_flight_provisioning", True, "0 execuções queued/in_progress/waiting/requested/pending")


def _check_grace(ctx: SharedContext, engine: str) -> Check:
    if ctx.leases_error:
        return Check("grace_period", False, "leases indisponíveis — fail-safe")
    stamps = [
        lv.decommissioned_at
        for lv in ctx.leases
        if lv.decommissioned_at and (engine == GCLB_ENGINE or engine in lv.engines)
    ]
    if not stamps:
        return Check("grace_period", True, "nenhum descomissionamento registrado para o engine")
    last = max(stamps)
    age_min = (ctx.now_epoch - last.timestamp()) / 60.0
    detail = f"último descomissionamento {last.strftime('%Y-%m-%dT%H:%M:%SZ')} (há {age_min:.0f} min; carência {ctx.grace_minutes} min)"
    return Check("grace_period", age_min >= ctx.grace_minutes, detail)


def _check_run_references(ctx: SharedContext, needles: Iterable[str]) -> Check:
    if ctx.run_inventory_error:
        return Check("cloud_run_references", False, f"inventário Cloud Run indisponível ({ctx.run_inventory_error}) — fail-safe")
    clean = sorted({n for n in needles if n and len(n) >= 6})
    refs = [item for item in ctx.run_inventory if any(n in item["blob"] for n in clean)]
    if refs:
        # Repositório público: só nomes de serviços da esteira (prefixo = slug de um lease); demais, contagem.
        slugs = ctx.lease_slugs
        own = sorted(f"{i['kind']} {i['region']}/{_safe(i['name'], 60)}" for i in refs if any(i["name"].startswith(s) for s in slugs))
        others = len(refs) - len(own)
        parts = own + ([f"{others} serviço(s)/job(s) fora da esteira (nomes omitidos no log público)"] if others else [])
        return Check("cloud_run_references", False, "referenciado por: " + ", ".join(parts))
    return Check(
        "cloud_run_references",
        True,
        f"{len(ctx.run_inventory)} serviço(s)/job(s) inspecionados; nenhum referencia o servidor (connection name, nome, segredo ou IP)",
    )


def _label_checks(labels: dict[str, Any]) -> list[Check]:
    managed = str(labels.get("managed_by", ""))
    opt_out = str(labels.get(OPT_OUT_LABEL, "")).strip().lower()
    return [
        Check(
            "managed_label",
            managed == MANAGED_BY_VALUE,
            f"managed_by={_safe(managed, 60) or '(ausente)'}" + ("" if managed == MANAGED_BY_VALUE else " — instância não gerenciada: nunca tocada"),
        ),
        Check("opt_out_label", opt_out not in OPT_OUT_VALUES, f"{OPT_OUT_LABEL}={_safe(opt_out, 30) or '(ausente)'}"),
    ]


def _describe_databases(databases: list[str], app_dbs: list[str]) -> str:
    """Somente nomes de sistema e no padrão da esteira (`db_<slug>`) aparecem no log público."""
    system = sorted(d for d in databases if d not in app_dbs)
    pipeline = sorted(d for d in app_dbs if PIPELINE_DB_RE.fullmatch(d))
    others = len(app_dbs) - len(pipeline)
    text = f"databases de sistema: {', '.join(system) or '(nenhum)'}"
    if app_dbs:
        text += " — de aplicação: " + ", ".join(pipeline + ([f"{others} fora do padrão db_<slug>"] if others else []))
    else:
        text += " (nenhum database de aplicação)"
    return text


def evaluate_sql(gcp: Gcp, ctx: SharedContext, engine: str) -> Evaluation:
    spec = SQL_SINGLETONS[engine]
    name = spec["instance"]
    ev = Evaluation(engine=engine, kind="cloudsql", instance=name)
    try:
        info = gcp.describe_sql(name)
    except ExternalQueryError as exc:
        ev.decision, ev.detail = "error", f"falha ao descrever a instância: {exc}"
        return ev
    if info is None:
        ev.decision, ev.detail = "absent", "instância não existe no projeto"
        return ev

    settings = info.get("settings") or {}
    labels = settings.get("userLabels") or {}
    ev.exists = True
    ev.state = _safe(info.get("state"), 30).upper()
    ev.activation_policy = _safe(settings.get("activationPolicy"), 20).upper()
    ev.tier = _safe(settings.get("tier"), 40)
    ev.checks.extend(_label_checks(labels))

    deletion_protection = bool(settings.get("deletionProtectionEnabled"))
    ev.checks.append(Check("deletion_protection", not deletion_protection, f"deletionProtectionEnabled={str(deletion_protection).lower()}"))
    if deletion_protection:
        ev.warnings.append(f"{name}: deletionProtectionEnabled=true — reaper respeita a proteção e não altera a instância.")

    if ev.activation_policy == "NEVER" or ev.state == "STOPPED":
        ev.decision = "already_stopped"
        ev.detail = "servidor já parado; será religado automaticamente pelo próximo deploy"
        return ev

    running = ev.state == "RUNNABLE" and ev.activation_policy == "ALWAYS"
    ev.checks.append(Check("running_state", running, f"state={ev.state or '?'}, activationPolicy={ev.activation_policy or '?'}"))

    try:
        pending = gcp.list_pending_sql_operations(name)
        ev.checks.append(
            Check(
                "pending_operations",
                not pending,
                "nenhuma operação pendente"
                if not pending
                else "pendentes: " + ", ".join(f"{_safe(op.get('operationType'), 30)}={_safe(op.get('status'), 15)}" for op in pending),
            )
        )
    except ExternalQueryError as exc:
        ev.checks.append(Check("pending_operations", False, f"consulta falhou ({exc}) — fail-safe"))

    ev.checks.append(_check_leases(ctx, engine))
    ev.checks.append(_check_in_flight(ctx))

    if running:
        try:
            databases = gcp.list_sql_databases(name)
            app_dbs = sorted(d for d in databases if d and d not in spec["system_databases"])
            ev.checks.append(Check("application_databases", not app_dbs, _describe_databases(databases, app_dbs)))
        except ExternalQueryError as exc:
            ev.checks.append(Check("application_databases", False, f"listagem falhou ({exc}) — fail-safe"))
    else:
        ev.checks.append(Check("application_databases", False, "não verificável com o servidor fora de RUNNABLE/ALWAYS"))

    needles = {name, str(info.get("connectionName") or ""), str(spec["secret_id"])}
    for ip in info.get("ipAddresses") or []:
        if str(ip.get("type", "")).upper() != "OUTGOING":
            needles.add(str(ip.get("ipAddress") or ""))
    ev.checks.append(_check_run_references(ctx, needles))
    ev.checks.append(_check_grace(ctx, engine))

    if ev.blocking:
        ev.decision = "in_use"
        ev.detail = "; ".join(f"{CHECK_LABELS.get(c.check_id, c.check_id)}: {c.detail}" for c in ev.blocking)
    else:
        ev.decision = "would_stop"
        ev.action = "stop"
        ev.detail = f"todas as {len(ev.checks)} verificações OK — servidor ocioso"
    return ev


def evaluate_redis(gcp: Gcp, ctx: SharedContext, region: str) -> Evaluation:
    ev = Evaluation(engine=REDIS_ENGINE, kind="memorystore-redis", instance=REDIS_INSTANCE)
    try:
        info = gcp.describe_redis(REDIS_INSTANCE, region)
    except ExternalQueryError as exc:
        ev.decision, ev.detail = "error", f"falha ao descrever o Redis: {exc}"
        return ev
    if info is None:
        ev.decision, ev.detail = "absent", f"Redis não existe em {region} (o Terraform recria sob demanda)"
        return ev

    ev.exists = True
    ev.state = _safe(info.get("state"), 30).upper()
    ev.tier = _safe(f"{info.get('tier', '?')} {info.get('memorySizeGb', '?')}GB", 40)
    ev.checks.extend(_label_checks(info.get("labels") or {}))
    ev.checks.append(Check("running_state", ev.state == "READY", f"state={ev.state or '?'}"))
    ev.checks.append(_check_leases(ctx, REDIS_ENGINE))
    ev.checks.append(_check_in_flight(ctx))
    ev.checks.append(_check_run_references(ctx, {REDIS_INSTANCE, REDIS_SECRET_ID, str(info.get("host") or ""), str(info.get("readEndpoint") or "")}))
    ev.checks.append(_check_grace(ctx, REDIS_ENGINE))

    if ev.blocking:
        ev.decision = "in_use"
        ev.detail = "; ".join(f"{CHECK_LABELS.get(c.check_id, c.check_id)}: {c.detail}" for c in ev.blocking)
    else:
        ev.decision = "would_delete"
        ev.action = "delete"
        ev.detail = (
            f"todas as {len(ev.checks)} verificações OK — Memorystore não pode ser parado; "
            "será apagado e recriado pelo Terraform no próximo deploy com Redis"
        )
    return ev


def collect_run_inventory(gcp: Gcp, regions: Sequence[str]) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for region in regions:
        for kind in ("services", "jobs"):
            for item in gcp.list_run_resources(kind, region):
                inventory.append(
                    {
                        "kind": "serviço" if kind == "services" else "job",
                        "region": region,
                        "name": str((item.get("metadata") or {}).get("name") or item.get("name") or "?"),
                        "blob": json.dumps(item, sort_keys=True),
                    }
                )
    return inventory


# ---------------------------------------------------------------------------------------------
# GCLB compartilhado (plano de borda `sdlc-shared-*`)
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class GclbStep:
    key: str
    label: str
    describe: tuple[str, ...]
    delete: tuple[str, ...]


def gclb_steps(bucket: str) -> list[GclbStep]:
    """Recursos do GCLB compartilhado em ORDEM REVERSA DE DEPENDÊNCIA (ordem segura de remoção)."""
    n = GCLB_NAMES

    def compute(kind: str, key: str, label: str, scope: tuple[str, ...] = ("--global",)) -> GclbStep:
        return GclbStep(
            key,
            f"{label} {n[key]}",
            ("compute", kind, "describe", n[key], *scope),
            ("compute", kind, "delete", n[key], *scope, "--quiet"),
        )

    def certmgr(kind: str, key: str, label: str) -> GclbStep:
        return GclbStep(
            key,
            f"{label} {n[key]}",
            ("certificate-manager", kind, "describe", n[key], "--location=global"),
            ("certificate-manager", kind, "delete", n[key], "--location=global", "--quiet"),
        )

    return [
        compute("forwarding-rules", "https_fwd", "forwarding rule"),
        compute("forwarding-rules", "http_fwd", "forwarding rule"),
        compute("target-https-proxies", "https_proxy", "target HTTPS proxy"),
        compute("target-http-proxies", "http_proxy", "target HTTP proxy"),
        compute("url-maps", "https_urlmap", "URL map"),
        compute("url-maps", "http_redirect", "URL map"),
        compute("backend-buckets", "fallback_backend", "backend bucket", scope=()),
        GclbStep(
            "fallback_bucket",
            f"bucket gs://{bucket}",
            ("storage", "buckets", "describe", f"gs://{bucket}"),
            ("storage", "rm", "--recursive", f"gs://{bucket}", "--quiet"),
        ),
        compute("addresses", "ip", "IP global"),
        certmgr("maps", "cert_map", "Certificate Map"),
        certmgr("certificates", "bootstrap_cert", "certificado"),
    ]


def _fallback_bucket(backend: dict[str, Any] | None) -> str:
    bucket = str((backend or {}).get("bucketName") or "")
    return bucket if bucket.startswith(GCLB_FALLBACK_BUCKET_PREFIX) else GCLB_DEFAULT_FALLBACK_BUCKET


def gclb_inventory(gcp: Gcp) -> dict[str, Any]:
    """Descreve os 12 recursos do GCLB compartilhado (None = ausente) + entradas do Certificate Map."""
    backend_step = next(s for s in gclb_steps(GCLB_DEFAULT_FALLBACK_BUCKET) if s.key == "fallback_backend")
    backend = gcp.describe_any(backend_step.describe)
    bucket = _fallback_bucket(backend)
    steps = gclb_steps(bucket)
    resources: dict[str, dict[str, Any] | None] = {}
    for step in steps:
        resources[step.key] = backend if step.key == "fallback_backend" else gcp.describe_any(step.describe)
    entries = gcp.list_cert_map_entries(GCLB_NAMES["cert_map"]) if resources.get("cert_map") is not None else []
    return {"bucket": bucket, "steps": steps, "resources": resources, "cert_map_entries": entries or []}


def _gclb_present_count(inventory: dict[str, Any]) -> int:
    present = sum(1 for v in inventory["resources"].values() if v is not None)
    if any(_basename(e.get("name")) == GCLB_NAMES["primary_cme"] for e in inventory["cert_map_entries"]):
        present += 1
    return present


def _basename(value: Any) -> str:
    return str(value or "").rstrip("/").rsplit("/", 1)[-1]


def _foreign_ip_users(address: dict[str, Any] | None) -> list[str]:
    own = {f"/forwardingRules/{GCLB_NAMES['https_fwd']}", f"/forwardingRules/{GCLB_NAMES['http_fwd']}"}
    return [u for u in (address or {}).get("users") or [] if not any(str(u).endswith(o) for o in own)]


def evaluate_gclb(gcp: Gcp, ctx: SharedContext) -> Evaluation:
    ev = Evaluation(engine=GCLB_ENGINE, kind="shared-gclb", instance=GCLB_LOGICAL_NAME)
    try:
        inv = gclb_inventory(gcp)
    except ExternalQueryError as exc:
        ev.decision, ev.detail = "error", f"falha ao inventariar o GCLB compartilhado: {exc}"
        return ev
    ev.extra["inventory"] = inv
    res = inv["resources"]
    present = _gclb_present_count(inv)
    if present == 0:
        ev.decision = "absent"
        ev.detail = f"nenhum dos {GCLB_TOTAL_RESOURCES} recursos do GCLB compartilhado existe (o próximo deploy recria todos)"
        return ev

    ev.exists = True
    ev.state = f"{present}/{GCLB_TOTAL_RESOURCES} recursos presentes"
    ip = res.get("ip") or {}
    ev.tier = f"IP {_safe(ip.get('address'), 40)}" if ip else "IP global ausente"

    labels: dict[str, Any] = {}
    for key in ("fallback_bucket", "ip", "https_fwd"):
        labels.update((res.get(key) or {}).get("labels") or {})
    ev.checks.extend(_label_checks(labels))
    ev.checks.append(_check_leases(ctx, GCLB_ENGINE))
    ev.checks.append(_check_in_flight(ctx))

    urlmap = res.get("https_urlmap")
    host_rules = (urlmap or {}).get("hostRules") or []
    if urlmap is None:
        ev.checks.append(Check("url_map_host_rules", True, f"{GCLB_NAMES['https_urlmap']} já ausente"))
    else:
        ev.checks.append(
            Check(
                "url_map_host_rules",
                not host_rules,
                f"hostRules={len(host_rules)}, pathMatchers={len((urlmap or {}).get('pathMatchers') or [])}"
                + (" — rotas de workloads ainda anexadas: GCLB mantido" if host_rules else ""),
            )
        )

    foreign = _foreign_ip_users(res.get("ip"))
    if res.get("ip") is None:
        ev.checks.append(Check("address_users", True, f"{GCLB_NAMES['ip']} já ausente"))
    else:
        ev.checks.append(
            Check(
                "address_users",
                not foreign,
                f"users={len((ip or {}).get('users') or [])}"
                + (f" — {len(foreign)} recurso(s) fora do GCLB compartilhado usam o IP" if foreign else " (só as forwarding rules do GCLB)"),
            )
        )

    orphans = [_basename(e.get("name")) for e in inv["cert_map_entries"] if _basename(e.get("name")) != GCLB_NAMES["primary_cme"]]
    slugs = {s[:24].rstrip("-") for s in ctx.lease_slugs}
    shown = sorted(_safe(o, 60) for o in orphans if any(o.startswith(s) for s in slugs))
    hidden = len(orphans) - len(shown)
    ev.checks.append(
        Check(
            "cert_map_entries",
            True,
            f"{len(inv['cert_map_entries'])} entrada(s)"
            + (f"; órfãs de workloads descomissionados serão removidas: {', '.join(shown + ([f'{hidden} outra(s)'] if hidden else []))}" if orphans else " (somente a bootstrap)"),
        )
    )
    ev.checks.append(_check_grace(ctx, GCLB_ENGINE))

    if ev.blocking:
        ev.decision = "in_use"
        ev.detail = "; ".join(f"{CHECK_LABELS.get(c.check_id, c.check_id)}: {c.detail}" for c in ev.blocking)
    else:
        ev.decision = "would_delete"
        ev.action = "teardown_gclb"
        ev.detail = (
            f"todas as {len(ev.checks)} verificações OK (0 workloads, 0 hostRules) — {present}/{GCLB_TOTAL_RESOURCES} recursos "
            "do GCLB compartilhado seriam removidos em ordem reversa de dependência (forwarding rules, proxies, URL maps, "
            "backend bucket, bucket, IP global, Certificate Map, certificado); o próximo deploy recria todos com IP novo"
        )
    return ev


def run_gclb_teardown(
    gcp: Gcp,
    *,
    log: Callable[[str], None],
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[str], str]:
    """Remove o GCLB compartilhado em ordem reversa de dependência, de forma idempotente.

    Retorna (resultado de cada passo, erro). Aborta antes de tocar no URL map HTTPS se ele tiver hostRules e
    antes do IP se outro recurso o usar. Recurso ausente = segue (remoções parciais convergem).
    """
    results: list[str] = []
    backend_step = next(s for s in gclb_steps(GCLB_DEFAULT_FALLBACK_BUCKET) if s.key == "fallback_backend")
    try:
        bucket = _fallback_bucket(gcp.describe_any(backend_step.describe))
    except ExternalQueryError as exc:
        return results, f"falha ao descrever {backend_step.label}: {exc}"

    def delete(label: str, args: tuple[str, ...]) -> str:
        for attempt in range(1, GCLB_DELETE_ATTEMPTS + 1):
            try:
                removed = gcp.delete_any(args)
                results.append(f"{label}: {'removido' if removed else 'já ausente'}")
                log(f"[FINOPS-REAPER] GCLB: {label} -> {'removido' if removed else 'já ausente'}")
                return ""
            except ExternalQueryError as exc:
                if attempt == GCLB_DELETE_ATTEMPTS:
                    return f"falha ao remover {label}: {exc}"
                log(f"[FINOPS-REAPER] GCLB: {label} falhou (tentativa {attempt}/{GCLB_DELETE_ATTEMPTS}); repetindo...")
                sleep(10 * attempt)
        return ""

    for step in gclb_steps(bucket):
        if step.key == "cert_map":
            try:
                entries = gcp.list_cert_map_entries(GCLB_NAMES["cert_map"]) or []
            except ExternalQueryError as exc:
                return results, f"falha ao listar entradas do Certificate Map: {exc}"
            for entry in entries:
                name = _basename(entry.get("name"))
                label = f"entrada {_safe(name, 60)} do Certificate Map"
                if dry_run:
                    results.append(f"(dry-run) apagaria {label}")
                    continue
                err = delete(label, ("certificate-manager", "maps", "entries", "delete", name, f"--map={GCLB_NAMES['cert_map']}", "--location=global", "--quiet"))
                if err:
                    return results, err
        try:
            current = gcp.describe_any(step.describe)
        except ExternalQueryError as exc:
            return results, f"falha ao descrever {step.label}: {exc}"
        if current is None:
            results.append(f"{step.label}: já ausente")
            continue
        if step.key == "https_urlmap" and current.get("hostRules"):
            return results, f"ABORTADO: {len(current['hostRules'])} hostRules em {GCLB_NAMES['https_urlmap']} (rota de workload anexada)"
        if step.key == "ip" and _foreign_ip_users(current):
            return results, f"ABORTADO: {GCLB_NAMES['ip']} ainda usado por recurso fora do GCLB compartilhado"
        if dry_run:
            results.append(f"(dry-run) apagaria {step.label}")
            continue
        err = delete(step.label, step.delete)
        if err:
            return results, err
    return results, ""


def repair_partial_gclb(gcp: Gcp, *, log: Callable[[str], None], dry_run: bool = False, sleep: Callable[[float], None] = time.sleep) -> int:
    """No deploy: se a forwarding rule HTTPS sumiu mas sobraram peças do GCLB, conclui a remoção.

    Sem isso, `reconcile_shared_state.sh` marca `shared_gclb_already_exists=false` e o Terraform tentaria criar
    recursos que ainda existem (409). Com a forwarding rule HTTPS ausente o GCLB não atende ninguém.
    """
    prefix = "[SHARED-GCLB-REPAIR]" + (" (dry-run)" if dry_run else "")
    try:
        inv = gclb_inventory(gcp)
    except ExternalQueryError as exc:
        log(f"::warning::{prefix} inventário do GCLB compartilhado falhou ({exc}); seguindo sem reparo.")
        return 0
    res = inv["resources"]
    present = _gclb_present_count(inv)
    if res.get("https_fwd") is not None:
        log(f"{prefix} GCLB compartilhado ativo ({present}/{GCLB_TOTAL_RESOURCES} recursos) -> reutilizado pelo deploy.")
        return 0
    if present == 0:
        log(f"{prefix} GCLB compartilhado ausente -> o Terraform recria os {GCLB_TOTAL_RESOURCES} recursos neste deploy.")
        return 0
    if (res.get("https_urlmap") or {}).get("hostRules"):
        log(f"::warning::{prefix} forwarding rule HTTPS ausente, mas {GCLB_NAMES['https_urlmap']} ainda tem hostRules; nada removido.")
        return 0
    log(
        f"{prefix} GCLB compartilhado incompleto (forwarding rule HTTPS ausente, {present} sobra(s)) -> concluindo a remoção "
        "para o Terraform recriar os 12 recursos sem conflito 409..."
    )
    results, err = run_gclb_teardown(gcp, log=log, dry_run=dry_run, sleep=sleep)
    for line in results:
        log(f"{prefix}   {line}")
    if err:
        log(f"::error::{prefix} {err}")
        return 1
    return 0


@dataclasses.dataclass
class ReaperDeps:
    """Dependências e parâmetros de uma execução (permite refazer o contexto antes de cada alteração)."""

    gcp: Gcp
    github: GitHub
    git_leases: GitLeases | None
    leases_dir: pathlib.Path | None
    region: str
    run_regions: Sequence[str]
    grace_minutes: int
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep


def build_context(deps: ReaperDeps, now_epoch: int | None = None) -> SharedContext:
    now = int(now_epoch if now_epoch is not None else deps.clock())
    # Ordem importa: runs em andamento ANTES dos leases. Um provisionamento que termine entre as duas
    # consultas já terá publicado o lease ACTIVE no main quando os leases forem lidos.
    in_flight: list[dict[str, Any]] = []
    in_flight_error = ""
    try:
        in_flight = deps.github.in_flight_runs()
    except ExternalQueryError as exc:
        in_flight_error = str(exc)

    leases = load_local_leases(deps.leases_dir, now)
    leases_error = ""
    if deps.git_leases is not None:
        try:
            for name, text in sorted(deps.git_leases.fetch_remote_leases().items()):
                leases.append(evaluate_lease(f"origin/main:{name}", text, now))
        except ExternalQueryError as exc:
            leases_error = str(exc)

    inventory: list[dict[str, Any]] = []
    inventory_error = ""
    try:
        inventory = collect_run_inventory(deps.gcp, deps.run_regions)
    except ExternalQueryError as exc:
        inventory_error = str(exc)

    return SharedContext(
        leases=leases,
        leases_error=leases_error,
        in_flight=in_flight,
        in_flight_error=in_flight_error,
        run_inventory=inventory,
        run_inventory_error=inventory_error,
        now_epoch=now,
        grace_minutes=deps.grace_minutes,
    )


def evaluate_all(deps: ReaperDeps, ctx: SharedContext) -> list[Evaluation]:
    evaluations = [evaluate_sql(deps.gcp, ctx, engine) for engine in SQL_SINGLETONS]
    evaluations.append(evaluate_redis(deps.gcp, ctx, deps.region))
    evaluations.append(evaluate_gclb(deps.gcp, ctx))
    return evaluations


def evaluate_one(deps: ReaperDeps, ctx: SharedContext, ev: Evaluation) -> Evaluation:
    if ev.kind == "cloudsql":
        return evaluate_sql(deps.gcp, ctx, ev.engine)
    if ev.kind == "shared-gclb":
        return evaluate_gclb(deps.gcp, ctx)
    return evaluate_redis(deps.gcp, ctx, deps.region)


# ---------------------------------------------------------------------------------------------
# Execução (somente fora do dry-run), com reavaliação COMPLETA imediatamente antes de alterar
# ---------------------------------------------------------------------------------------------


def execute_action(
    deps: ReaperDeps,
    ev: Evaluation,
    log: Callable[[str], None],
    wait_timeout_s: int = 0,
    now_epoch: int | None = None,
) -> tuple[Evaluation, bool]:
    """Refaz contexto + avaliação do servidor do zero; só altera se continuar ocioso.

    Retorna (avaliação final, sucesso). A parada do Cloud SQL é assíncrona por padrão (a próxima execução
    confirma `JÁ PARADO`); `wait_timeout_s > 0` espera a operação terminar.
    """
    fresh = evaluate_one(deps, build_context(deps, now_epoch), ev)
    if fresh.action != ev.action:
        fresh.warnings.extend(ev.warnings)
        if fresh.decision not in {"error"}:
            fresh.detail = f"revalidação completa antes de alterar: {fresh.detail}"
            fresh.decision, fresh.action = "skipped_race", ""
        log(f"[FINOPS-REAPER] {ev.instance}: ação cancelada na revalidação — {fresh.detail}")
        return fresh, True
    # Última barreira barata imediatamente antes da chamada de alteração.
    try:
        runs = deps.github.in_flight_runs()
    except ExternalQueryError as exc:
        fresh.decision, fresh.action, fresh.detail = "skipped_race", "", f"API do GitHub indisponível na última checagem ({exc})"
        return fresh, True
    if runs:
        fresh.decision, fresh.action = "skipped_race", ""
        fresh.detail = "provisionamento iniciado durante a revalidação: " + ", ".join(f"#{r.get('id')}" for r in runs)
        log(f"[FINOPS-REAPER] {ev.instance}: ação cancelada — {fresh.detail}")
        return fresh, True

    if fresh.action == "stop":
        try:
            log(f"[FINOPS-REAPER] Parando {fresh.instance} (gcloud sql instances patch --activation-policy=NEVER --async)...")
            fresh.operation = deps.gcp.set_sql_activation_policy(fresh.instance, "NEVER")
        except ExternalQueryError as exc:
            fresh.decision, fresh.detail = "error", f"falha ao parar: {exc}"
            return fresh, False
        fresh.decision = "stopped"
        fresh.detail = f"parada solicitada (operação {fresh.operation or 'n/d'}); religamento automático no próximo deploy"
        if wait_timeout_s > 0 and fresh.operation:
            try:
                deps.gcp.wait_sql_operation(fresh.operation, wait_timeout_s)
                info = deps.gcp.describe_sql(fresh.instance) or {}
                policy = str((info.get("settings") or {}).get("activationPolicy", "")).upper()
                fresh.detail = f"parada confirmada (activation-policy={policy or '?'}, state={_safe(info.get('state'), 20) or '?'}); religamento automático no próximo deploy"
                if policy != "NEVER":
                    fresh.warnings.append(f"{fresh.instance}: após a operação o activation-policy ainda é {policy or '?'}.")
            except ExternalQueryError as exc:
                fresh.warnings.append(f"{fresh.instance}: parada solicitada, mas a confirmação falhou ({exc}).")
        return fresh, True

    if fresh.action == "delete":
        try:
            log(f"[FINOPS-REAPER] Apagando {fresh.instance} (gcloud redis instances delete --async)...")
            fresh.operation = deps.gcp.delete_redis(fresh.instance, deps.region)
        except ExternalQueryError as exc:
            fresh.decision, fresh.detail = "error", f"falha ao apagar: {exc}"
            return fresh, False
        fresh.decision = "deleted"
        fresh.detail = (
            f"remoção solicitada (operação {fresh.operation or 'n/d'}); deploys esperam o fim da remoção e o "
            "Terraform recria o Redis no próximo deploy que pedir Redis"
        )
        return fresh, True

    if fresh.action == "teardown_gclb":
        log("[FINOPS-REAPER] Removendo o GCLB compartilhado (ordem reversa de dependência, idempotente)...")
        results, err = run_gclb_teardown(deps.gcp, log=log, sleep=deps.sleep)
        fresh.extra["teardown_results"] = results
        removed = sum(1 for r in results if r.endswith(": removido"))
        if err:
            fresh.decision = "error"
            fresh.detail = f"remoção do GCLB interrompida ({removed} recurso(s) removido(s) antes): {err}; a próxima execução continua de onde parou"
            return fresh, False
        fresh.decision = "deleted"
        fresh.detail = (
            f"{removed} recurso(s) removido(s), {len(results) - removed} já ausente(s): 0 forwarding rules e 0 IP global "
            "do GCLB compartilhado; o próximo deploy recria os 12 recursos (IP novo)"
        )
        return fresh, True
    return fresh, True


# ---------------------------------------------------------------------------------------------
# Orquestração + relatórios
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass
class ReaperReport:
    project: str
    mode: str
    generated_at_utc: str
    grace_minutes: int
    context: SharedContext
    evaluations: list[Evaluation]
    shared_gclb: dict[str, Any]
    exit_code: int = EXIT_OK

    @property
    def fail_safe_reasons(self) -> list[str]:
        reasons = []
        if self.context.leases_error:
            reasons.append(f"leases (origin/main): {self.context.leases_error}")
        if self.context.in_flight_error:
            reasons.append(f"runs de provisionamento: {self.context.in_flight_error}")
        if self.context.run_inventory_error:
            reasons.append(f"inventário Cloud Run: {self.context.run_inventory_error}")
        reasons.extend(f"lease ilegível: {lv.source}" for lv in self.context.leases if lv.status == "ILEGIVEL")
        reasons.extend(f"{ev.instance}: {ev.detail}" for ev in self.evaluations if ev.decision == "error" and not ev.action)
        return reasons

    def instances_with(self, *decisions: str) -> list[str]:
        return [ev.instance for ev in self.evaluations if ev.decision in decisions]

    def to_json(self) -> dict[str, Any]:
        ctx = self.context
        return {
            "project": self.project,
            "mode": self.mode,
            "generated_at_utc": self.generated_at_utc,
            "grace_minutes": self.grace_minutes,
            "policy": {
                "cloud_sql": "STOP (activation-policy NEVER); nunca apagar (nome reservado ~7 dias)",
                "memorystore_redis": "DELETE só sem nenhum lease em uso (Terraform recria sob demanda)",
                "shared_gclb": "DELETE dos 12 recursos (0 workloads, 0 hostRules); o próximo deploy recria (IP novo)",
                "auto_resume": "scripts/provision_workload.sh religa (ALWAYS) e espera RUNNABLE antes do Terraform",
            },
            "fail_safe": bool(self.fail_safe_reasons),
            "fail_safe_reasons": self.fail_safe_reasons,
            "in_flight_provisioning_runs": ctx.in_flight,
            "leases": [lv.to_json() for lv in ctx.leases],
            "cloud_run_inventory_count": len(ctx.run_inventory),
            "cloud_run_regions": sorted({i["region"] for i in ctx.run_inventory}),
            "evaluations": [ev.to_json() for ev in self.evaluations],
            "shared_gclb": self.shared_gclb,
            "reaped_instances": self.instances_with("stopped", "deleted"),
            "would_reap_instances": self.instances_with("would_stop", "would_delete"),
            "exit_code": self.exit_code,
        }


def run_reaper(
    deps: ReaperDeps,
    *,
    dry_run: bool = True,
    now_epoch: int | None = None,
    log: Callable[[str], None] = print,
    wait_timeout_s: int = 0,
) -> ReaperReport:
    ctx = build_context(deps, now_epoch)
    evaluations = evaluate_all(deps, ctx)

    exit_code = EXIT_OK
    if not dry_run:
        for idx, ev in enumerate(evaluations):
            if not ev.action:
                continue
            final, ok = execute_action(deps, ev, log, wait_timeout_s=wait_timeout_s, now_epoch=now_epoch)
            evaluations[idx] = final
            if not ok:
                exit_code = EXIT_ACTION_FAILED

    gclb_ev = next((e for e in evaluations if e.kind == "shared-gclb"), None)
    gclb: dict[str, Any] = {
        "logical_name": GCLB_LOGICAL_NAME,
        "decision": gclb_ev.decision if gclb_ev else "n/d",
        "state": gclb_ev.state if gclb_ev else "",
        "teardown_results": (gclb_ev.extra.get("teardown_results", []) if gclb_ev else []),
    }

    report = ReaperReport(
        project=deps.gcp.project,
        mode="dry-run" if dry_run else "live",
        generated_at_utc=_utc_iso(ctx.now_epoch),
        grace_minutes=deps.grace_minutes,
        context=ctx,
        evaluations=evaluations,
        shared_gclb=gclb,
    )
    if exit_code == EXIT_OK and report.fail_safe_reasons:
        exit_code = EXIT_FAIL_SAFE
    report.exit_code = exit_code
    return report


def render_text(report: ReaperReport) -> str:
    ctx = report.context
    mode = "DRY-RUN (nenhuma alteração)" if report.mode == "dry-run" else "EXECUÇÃO (alterações reais)"
    in_use_leases = [lv for lv in ctx.leases if lv.in_use]
    lines = [
        f"[FINOPS-REAPER] Projeto {report.project} | modo: {mode} | carência: {report.grace_minutes} min | {report.generated_at_utc}",
        f"[FINOPS-REAPER] Leases lidos (checkout + origin/main): {len(ctx.leases)} (em uso: {len(in_use_leases)})"
        + (f" | ERRO leases: {ctx.leases_error}" if ctx.leases_error else ""),
        "[FINOPS-REAPER] Provisionamentos em andamento: " + (f"ERRO ({ctx.in_flight_error})" if ctx.in_flight_error else str(len(ctx.in_flight))),
        "[FINOPS-REAPER] Inventário Cloud Run: "
        + (f"ERRO ({ctx.run_inventory_error})" if ctx.run_inventory_error else f"{len(ctx.run_inventory)} serviço(s)/job(s)"),
    ]
    for ev in report.evaluations:
        header = f"[FINOPS-REAPER] {ev.instance} ({ev.engine}, {ev.kind})"
        if ev.exists:
            header += (
                f" [state={ev.state or '?'}"
                + (f", activationPolicy={ev.activation_policy}" if ev.activation_policy else "")
                + (f", {ev.tier}" if ev.tier else "")
                + "]"
            )
        lines.append(f"{header} -> {decision_label(ev.kind, ev.decision)}")
        for chk in ev.checks:
            lines.append(f"    [{'OK' if chk.ok else 'BLOQUEIA'}] {CHECK_LABELS.get(chk.check_id, chk.check_id)}: {chk.detail}")
        if ev.detail:
            lines.append(f"    => {ev.detail}")
        if ev.kind == "memorystore-redis" and ev.exists:
            lines.append("    (databases do Redis não são verificáveis a partir do runner: valem leases + referências Cloud Run)")
        for line in ev.extra.get("teardown_results", []):
            lines.append(f"    - {line}")
    reaped = report.instances_with("stopped", "deleted")
    would = report.instances_with("would_stop", "would_delete")
    lines.append(
        "[FINOPS-REAPER] Resumo: "
        + (f"alterados agora: {', '.join(reaped) or 'nenhum'}" if report.mode == "live" else f"seriam alterados: {', '.join(would) or 'nenhum'}")
        + (" | FAIL-SAFE ativo: " + " | ".join(report.fail_safe_reasons) if report.fail_safe_reasons else "")
    )
    return "\n".join(lines)


def render_step_summary(report: ReaperReport) -> str:
    ctx = report.context
    mode = "DRY-RUN (nenhuma alteração)" if report.mode == "dry-run" else "EXECUÇÃO"
    rows = [
        f"## FinOps — servidores compartilhados ociosos (`{report.project}`)",
        "",
        f"- Modo: **{mode}** · carência pós-descomissionamento: **{report.grace_minutes} min** · "
        f"provisionamentos em andamento: **{'ERRO' if ctx.in_flight_error else len(ctx.in_flight)}** · "
        f"leases em uso: **{sum(1 for lv in ctx.leases if lv.in_use)}/{len(ctx.leases)}**",
        "- Política: Cloud SQL ocioso é **parado** (`activation-policy NEVER`, sem custo de vCPU/RAM; nunca apagado — o nome "
        "ficaria reservado ~7 dias); Memorystore Redis ocioso é **apagado** (só sem nenhum lease em uso; o Terraform recria); "
        "GCLB compartilhado (forwarding rules, proxies, URL maps, bucket de fallback, IP global, Certificate Map) é "
        "**removido** quando não há nenhum workload (0 hostRules); o próximo deploy **religa/recria tudo** antes do Terraform.",
        "",
        "| Servidor | Engine | Estado | Decisão | Detalhe |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for ev in report.evaluations:
        state = f"{ev.state}/{ev.activation_policy}" if ev.activation_policy else (ev.state or "-")
        detail = ev.detail.replace("|", "\\|")
        rows.append(f"| `{ev.instance}` | {ev.engine} | {state} | **{decision_label(ev.kind, ev.decision)}** | {detail} |")
    rows.append("")
    if report.fail_safe_reasons:
        rows.append("- **Fail-safe ativo** (nenhuma ação nos itens afetados; execução marcada com falha para ficar visível): " + "; ".join(report.fail_safe_reasons))
    return "\n".join(rows) + "\n"


def write_github_output(path: str, report: ReaperReport) -> None:
    reaped = report.instances_with("stopped", "deleted")
    would = report.instances_with("would_stop", "would_delete")
    kept = report.instances_with("in_use", "skipped_race", "error")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"reaper_mode={report.mode}\n")
        fh.write(f"reaped_count={len(reaped)}\n")
        fh.write(f"reaped_instances={','.join(reaped)}\n")
        fh.write(f"would_reap_count={len(would)}\n")
        fh.write(f"would_reap_instances={','.join(would)}\n")
        fh.write(f"kept_running_instances={','.join(kept)}\n")
        fh.write(f"stopped_instances={','.join(report.instances_with('already_stopped', 'stopped'))}\n")
        fh.write(f"reaper_fail_safe={'true' if report.fail_safe_reasons else 'false'}\n")


# ---------------------------------------------------------------------------------------------
# Religamento automático (chamado pelo provision_workload.sh antes do Terraform)
# ---------------------------------------------------------------------------------------------


def _sql_status(info: dict[str, Any]) -> tuple[str, str]:
    settings = info.get("settings") or {}
    return str(info.get("state") or "").upper(), str(settings.get("activationPolicy") or "").upper()


def _warn_if_stopped_outside_pipeline(gcp: Gcp, name: str, prefix: str, log: Callable[[str], None], pipeline_sa: str) -> None:
    """Avisa (sem expor identidades) quando a última alteração da instância não foi feita pela esteira."""
    try:
        ops = gcp.list_sql_operations(name)
    except ExternalQueryError:
        return
    updates = [op for op in ops if str(op.get("operationType", "")).upper() == "UPDATE" and str(op.get("status", "")).upper() == "DONE"]
    if updates and str(updates[0].get("user", "")) not in {pipeline_sa, ""}:
        log(
            f"::warning::{prefix} '{name}' foi parada por uma identidade fora da esteira (última operação UPDATE em "
            f"{_safe(updates[0].get('endTime') or updates[0].get('insertTime'), 40)}); religando conforme a política de deploy."
        )


def resume_shared_backing(
    gcp: Gcp,
    engine: str,
    *,
    enable_redis: bool = False,
    region: str = DEFAULT_REGION,
    phase: str = "full",
    timeout_s: int = 600,
    poll_s: int = 15,
    dry_run: bool = False,
    pipeline_sa: str = PIPELINE_SERVICE_ACCOUNT,
    repair_gclb: bool = True,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Religa o singleton Cloud SQL parado por ociosidade e espera os singletons ficarem utilizáveis.

    phase='begin': só dispara o religamento (não bloqueia; nunca falha o deploy).
    phase='wait': (re)dispara se preciso e espera Cloud SQL RUNNABLE+ALWAYS sem operações pendentes; com
                  Redis, espera também uma remoção em andamento terminar (o Terraform então o recria).
                  Timeout -> rc 1 (deploy interrompido antes do Terraform).
    phase='full': begin + wait.
    """
    canonical = normalize_engine(engine)
    sql_name = SQL_SINGLETONS[canonical]["instance"] if canonical in SQL_SINGLETONS else ""
    prefix = "[SHARED-DB-RESUME]" + (" (dry-run)" if dry_run else "")
    if not sql_name:
        log(f"{prefix} database_type='{_safe(engine, 30)}' não usa servidor Cloud SQL singleton -> nada a religar.")

    if sql_name and phase in {"begin", "full"}:
        try:
            info: dict[str, Any] | None = gcp.describe_sql(sql_name)
        except ExternalQueryError as exc:
            log(f"::warning::{prefix} Não foi possível consultar '{sql_name}' ({exc}); nova verificação antes do Terraform.")
            info = {}
        if info is None:
            log(f"{prefix} '{sql_name}' ainda não existe -> será criada pelo Terraform (primeiro deploy deste engine).")
        elif info:
            state, policy = _sql_status(info)
            if policy == "NEVER" or state == "STOPPED":
                log(
                    f"{prefix} '{sql_name}' está PARADA por ociosidade (FinOps idle reaper, activation-policy=NEVER) "
                    "-> religando (activation-policy=ALWAYS) em paralelo ao build..."
                )
                _warn_if_stopped_outside_pipeline(gcp, sql_name, prefix, log, pipeline_sa)
                if dry_run:
                    log(f"{prefix} executaria: gcloud sql instances patch {sql_name} --activation-policy=ALWAYS --async --quiet --project={gcp.project}")
                else:
                    try:
                        if gcp.list_pending_sql_operations(sql_name):
                            log(f"{prefix} operação em andamento em '{sql_name}'; o religamento será (re)tentado antes do Terraform.")
                        else:
                            op = gcp.set_sql_activation_policy(sql_name, "ALWAYS")
                            log(f"{prefix} religamento de '{sql_name}' solicitado (operação {op or 'n/d'}).")
                    except ExternalQueryError as exc:
                        log(f"::warning::{prefix} religamento de '{sql_name}' não pôde ser solicitado agora ({exc}); nova tentativa antes do Terraform.")
            else:
                log(f"{prefix} '{sql_name}' já está em execução (state={state}, activation-policy={policy}) -> nada a religar.")
    if phase == "begin":
        return 0

    started = clock()
    deadline = started + max(0, int(timeout_s))
    last_status = ""
    polls = 0
    sql_ready = not sql_name
    redis_ready = not enable_redis
    while True:
        polls += 1
        status_parts: list[str] = []
        if not sql_ready:
            try:
                info = gcp.describe_sql(sql_name)
                if info is None:
                    log(f"{prefix} '{sql_name}' não existe -> será criada pelo Terraform.")
                    sql_ready = True
                else:
                    state, policy = _sql_status(info)
                    pending = gcp.list_pending_sql_operations(sql_name)
                    if policy == "NEVER" or state == "STOPPED":
                        if dry_run:
                            log(
                                f"{prefix} '{sql_name}' parada: executaria 'gcloud sql instances patch {sql_name} --activation-policy=ALWAYS' "
                                f"e aguardaria RUNNABLE por até {int(timeout_s) // 60} min antes do Terraform."
                            )
                            sql_ready = True
                        elif not pending:
                            try:
                                op = gcp.set_sql_activation_policy(sql_name, "ALWAYS")
                                log(f"{prefix} religamento de '{sql_name}' solicitado (operação {op or 'n/d'}).")
                            except ExternalQueryError as exc:
                                log(f"{prefix} tentativa de religar '{sql_name}' falhou ({exc}); repetindo.")
                    elif state == "RUNNABLE" and policy == "ALWAYS" and not pending:
                        log(
                            f"{prefix} '{sql_name}' RUNNABLE (activation-policy=ALWAYS)"
                            + (f" após {int(clock() - started)}s de espera" if polls > 1 else "")
                            + " -> seguindo para o Terraform."
                        )
                        sql_ready = True
                    if not sql_ready:
                        status_parts.append(f"{sql_name}: state={state or '?'}, activation-policy={policy or '?'}, operações pendentes={len(pending)}")
            except ExternalQueryError as exc:
                status_parts.append(f"{sql_name}: consulta falhou ({_safe(str(exc), 160)})")
        if not redis_ready:
            try:
                rinfo = gcp.describe_redis(REDIS_INSTANCE, region)
                rstate = str((rinfo or {}).get("state") or "").upper()
                if rinfo is None:
                    log(f"{prefix} '{REDIS_INSTANCE}' não existe -> será (re)criado pelo Terraform neste deploy.")
                    redis_ready = True
                elif rstate in REDIS_TRANSITIONAL_STATES:
                    if dry_run:
                        log(f"{prefix} '{REDIS_INSTANCE}' em {rstate}: aguardaria terminar antes do Terraform.")
                        redis_ready = True
                    else:
                        status_parts.append(f"{REDIS_INSTANCE}: state={rstate} (aguardando terminar antes do Terraform)")
                else:
                    log(f"{prefix} '{REDIS_INSTANCE}' state={rstate or '?'} -> seguindo para o Terraform.")
                    redis_ready = True
            except ExternalQueryError as exc:
                status_parts.append(f"{REDIS_INSTANCE}: consulta falhou ({_safe(str(exc), 160)})")
        if sql_ready and redis_ready:
            return repair_partial_gclb(gcp, log=log, dry_run=dry_run, sleep=sleep) if repair_gclb else 0
        status_line = " | ".join(status_parts)
        if status_line != last_status:
            log(f"{prefix} {dt.datetime.now(dt.timezone.utc).strftime('%H:%M:%S')} aguardando: {status_line}")
            last_status = status_line
        if clock() >= deadline:
            log(
                f"::error::{prefix} singletons não ficaram prontos em {int(timeout_s) // 60} min ({status_line}). "
                "Deploy interrompido antes do Terraform (o database/Redis da aplicação não pode ser criado agora)."
            )
            return 1
        sleep(poll_s)


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1]


def _parse_reap_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Para/apaga servidores singleton compartilhados ociosos (FinOps).")
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--dry-run", type=str_to_bool, default=True, help="true (padrão) = só relata; false = aplica")
    parser.add_argument("--grace-minutes", type=int, default=DEFAULT_GRACE_MINUTES)
    parser.add_argument("--github-output", default="", help="Caminho do $GITHUB_OUTPUT (opcional)")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO)
    parser.add_argument("--repo-dir", default=str(_repo_root()), help="Checkout git do repositório de governança")
    parser.add_argument("--region", default=DEFAULT_REGION, help="Região do Memorystore Redis singleton")
    parser.add_argument("--run-regions", default=",".join(DEFAULT_RUN_REGIONS), help="Regiões Cloud Run inspecionadas")
    parser.add_argument("--leases-dir", default=str(_repo_root() / "active-leases"))
    parser.add_argument("--remote-leases", type=str_to_bool, default=True, help="Também ler active-leases/ do origin/main (git fetch)")
    parser.add_argument("--json-out", default="", help="Grava o relatório JSON neste caminho (opcional)")
    parser.add_argument("--wait-timeout-seconds", type=int, default=0, help="0 = parada assíncrona (padrão); >0 espera confirmar")
    return parser.parse_args(list(argv))


def _parse_resume_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Religa singletons parados/removidos por ociosidade antes do deploy.")
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--engine", required=True, help="database_type do workload (postgresql/mysql/sqlserver/none/...)")
    parser.add_argument("--enable-redis", type=str_to_bool, default=False)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--phase", choices=("begin", "wait", "full"), default="full")
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--poll-seconds", type=int, default=15)
    parser.add_argument("--dry-run", type=str_to_bool, default=False)
    parser.add_argument("--repair-gclb", type=str_to_bool, default=True, help="Concluir remoção parcial do GCLB compartilhado (fase wait)")
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None, runner: Runner = subprocess_runner) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] == "resume":
        rargs = _parse_resume_args(args_list[1:])
        if rargs.project != DEFAULT_PROJECT:
            print(f"ERRO DE GOVERNANÇA: projeto '{rargs.project}' não autorizado (permitido: {DEFAULT_PROJECT}).", file=sys.stderr)
            return EXIT_USAGE
        return resume_shared_backing(
            Gcp(rargs.project, runner),
            rargs.engine,
            enable_redis=rargs.enable_redis,
            region=rargs.region,
            phase=rargs.phase,
            timeout_s=rargs.timeout_seconds,
            poll_s=rargs.poll_seconds,
            dry_run=rargs.dry_run,
            repair_gclb=rargs.repair_gclb,
        )
    if args_list and args_list[0] == "reap":
        args_list = args_list[1:]
    args = _parse_reap_args(args_list)
    if args.project != DEFAULT_PROJECT:
        print(f"ERRO DE GOVERNANÇA: projeto '{args.project}' não autorizado (permitido: {DEFAULT_PROJECT}).", file=sys.stderr)
        return EXIT_USAGE

    deps = ReaperDeps(
        gcp=Gcp(args.project, runner),
        github=GitHub(args.repo, runner),
        git_leases=GitLeases(pathlib.Path(args.repo_dir), runner) if args.remote_leases else None,
        leases_dir=pathlib.Path(args.leases_dir) if args.leases_dir else None,
        region=args.region,
        run_regions=[r.strip() for r in args.run_regions.split(",") if r.strip()],
        grace_minutes=max(0, args.grace_minutes),
    )
    report = run_reaper(deps, dry_run=args.dry_run, wait_timeout_s=max(0, args.wait_timeout_seconds))
    print(render_text(report))
    for ev in report.evaluations:
        for warning in ev.warnings:
            print(f"::warning::[FINOPS-REAPER] {warning}")
    if report.fail_safe_reasons:
        print("::warning::[FINOPS-REAPER] Fail-safe ativo (consulta externa falhou): nenhum servidor afetado foi alterado; "
              "a execução sai com código 3 para a falha ficar visível.")
    payload = json.dumps(report.to_json(), ensure_ascii=False, indent=2)
    print("--- RELATÓRIO JSON (FINOPS-REAPER) ---")
    print(payload)
    if args.json_out:
        pathlib.Path(args.json_out).write_text(payload + "\n", encoding="utf-8")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(render_step_summary(report))
    if args.github_output:
        write_github_output(args.github_output, report)
    if report.exit_code == EXIT_ACTION_FAILED:
        print("::error::[FINOPS-REAPER] Uma ou mais ações de parada/remoção falharam (ver detalhes acima).")
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
