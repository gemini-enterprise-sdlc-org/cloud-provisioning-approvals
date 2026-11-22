# Cloud Provisioning Approvals — Governança FinOps, Tag `GCP_DEPLOYED_VERSION` & TTL (`ernani-demo-argolis`)

Repositório central e auditável de aprovação humana (**Human-in-the-Loop**) para provisionamento e descomissionamento automático de *workloads* gerados ou revisados pela **Fábrica de Software Agêntica Governada — Enterprise Architecture & Platform Engineering (EA/PE)** no **Gemini Enterprise**.

## Regras Inegociáveis de Governança (Sandbox Compartilhado Isolado)

1. **Repositório Limpo (`main` Intocada + Tag `GCP_DEPLOYED_VERSION` + Trava SHA Anti-Tampering)**:
   - O Agente pode revisar e preparar **qualquer repositório de aplicação** em `gemini-enterprise-sdlc-org` sem duplicar repositórios (`gcp-*`).
   - As adaptações Cloud Run + Cloud SQL + Terraform são gravadas na branch dedicada `gcp/sandbox` e marcadas com a tag anotada **`GCP_DEPLOYED_VERSION`**, mantendo a `main` 100% intocada e exibindo no GitHub (`/compare/main...GCP_DEPLOYED_VERSION`) o diff completo.
   - **Pinagem Dupla Anti-Tampering (TOCTOU)**: O `approval-manifest.json` exige simultaneamente `git_tag: "GCP_DEPLOYED_VERSION"` e `expected_commit_sha`.
2. **Argumentos Obrigatórios Forçados (`budget_brl` e `ttl`)**:
   - Nenhum deployment é aceito pela Service Account (`sdlc-provisioner-sa@ernani-demo-argolis.iam.gserviceaccount.com`) sem **Orçamento Máximo (`budget_brl > 0`)** e **Tempo de Vida (`ttl`, padrão `1h` contado após a validação HTTPS dos 3 hosts `.nip.io`, ou `15d` para sandbox)**.
3. **Isolamento Estrito `sdlc-shared-*` no Projeto `ernani-demo-argolis`**:
   - Todos os singletons gerenciados usam prefixo dedicado `sdlc-shared-*` (`sdlc-shared-pgsql`, `sdlc-shared-mysql`, `sdlc-shared-sqlserver`, `sdlc-shared-redis`, `sdlc-shared-gclb-*`) e rótulo `managed_by=cloud-provisioning-approvals`. Recursos externos no projeto jamais são inspecionados ou alterados.

## Desprovisionar antes do prazo ou alterar o TTL (somente aprovadores EA/PE)

- Os aprovadores listados em `policies/governance-policy.json` (`lifecycle_override_approvers`) podem solicitar à *Fábrica de Software Agêntica Governada* para **desprovisionar agora** um ambiente ativo ou **alterar o seu TTL** (entre `5m` e `24h`).
- A identidade vem da sessão autenticada do Gemini Enterprise. O agente grava em `lifecycle-requests/` um pedido com selo **HMAC-SHA256** processado por `workload-lifecycle-override.yml`.
