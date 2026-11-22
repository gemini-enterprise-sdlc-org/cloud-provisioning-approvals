# Modelos de Aprovação de Pull Requests de Provisionamento (GitOps & Four-Eyes)

## Contexto do Problema (Auto-Aprovação Bloqueada pelo GitHub)

No GitHub, **uma conta de usuário nunca pode submeter um review `Approve` em um Pull Request aberto pela própria conta** (*"Reviewers cannot approve their own pull request"*).

Quando o runtime do Agente (`sdlc-factory`) usa um **Personal Access Token (Fine-Grained PAT)** emitido pela mesma conta humana listada em `CODEOWNERS` (ex.: `@ernani`), o GitHub registra `@ernani` como **criador do PR**. Caso a Branch Protection da `main` exija `required_approving_review_count >= 1`, ocorre um *deadlock* operacional em ambientes de dono único (ou quando o emissor do PAT é o mesmo revisor que tenta aprovar).

A plataforma suporta nativamente **3 modelos arquiteturais oficiais** para resolver isso de forma limpa e auditável — aplicáveis tanto ao sandbox quanto a ambientes corporativos de clientes.

---

## Modelo 1 (Recomendado para Produção & Enterprise): GitHub App Próprio da Fábrica (`[bot]`)

Neste modelo, o Agente **não usa o PAT de uma pessoa física**. Ele autentica como um **GitHub App da organização** (ex.: `sdlc-factory-bot`), gerando *Installation Access Tokens* efêmeros (1 hora de vida) via chave RSA privada no Secret Manager.

- **Quem aparece como Criador do PR:** `sdlc-factory-bot[bot]`
- **Quem aprova o PR (`CODEOWNERS`):** `@ernani` ou time `@<org>/cloud-approvers`
- **Resultado no GitHub UI:** como o criador é `sdlc-factory-bot[bot]`, o botão **`Approve`** fica **100% liberado** para o próprio dono da organização (`@ernani`), mantendo `required_approving_review_count: 1`, `require_code_owner_reviews: true` e `enforce_admins: true`!

### Passo a passo para ativar (2 minutos)

1. Na organização GitHub, acesse:
   `Organization Settings -> Developer settings -> GitHub Apps -> New GitHub App`
2. Preencha:
   - **GitHub App name:** `sdlc-factory-bot-<org>` (nome único global no GitHub)
   - **Homepage URL:** `https://github.com/<org>/cloud-provisioning-approvals`
   - **Webhook:** desmarque *Active* (não é necessário webhook de entrada)
   - **Repository permissions:**
     - `Administration`: **Read and write** *(criar repositórios de novas apps)*
     - `Contents`: **Read and write** *(clonar skills, commitar código, tags e branches)*
     - `Pull requests`: **Read and write** *(abrir PRs de aprovação e evolução Day-2)*
     - `Workflows`: **Read and write** *(publicar `.github/workflows/*.yml` das apps)*
     - `Metadata`: **Read-only**
   - **Where can this GitHub App be installed?** *Only on this account*
3. Clique em **Create GitHub App**, anote o **`App ID`**, clique em **Generate a private key** (baixa o `.pem`) e em **Install App** na organização (`All repositories`), anotando o **`Installation ID`** final da URL (`/settings/installations/<INSTALLATION_ID>`).
4. Grave a chave `.pem` no Secret Manager e configure as 3 variáveis no Agente (`Cloud Run` + `Agent Engine`):
   ```bash
   gcloud secrets create sdlc-github-app-pem --project=<GCP_PROJECT> --data-file=private-key.pem
   # Variáveis suportadas nativamente por github_cloud_gtm_pusher._resolve_github_token():
   # GITHUB_APP_ID=<APP_ID>
   # GITHUB_APP_INSTALLATION_ID=<INSTALLATION_ID>
   # GITHUB_APP_PRIVATE_KEY_SECRET_NAME=projects/<GCP_PROJECT>/secrets/sdlc-github-app-pem/versions/latest
   ```
5. Habilite a proteção estrita com `required_approving_review_count: 1` e `require_code_owner_reviews: true`:
   - O robô `sdlc-factory-bot[bot]` abre o PR; você revisa e clica normalmente em **Approve** + **Merge**.
   - **Observação para o Selo HMAC (`provision_workload.sh`):** mantenha também o segredo `sdlc-github-org-pat` (ou configure `MANIFEST_HMAC_SECRET_NAME`) sincronizado entre o Agente e o GitHub Actions WIF para a verificação criptográfica `HMAC-SHA256` do `approval-manifest.json`.

---

## Modelo 2 (Alternativa Simples para Equipes / Clientes): Conta de Serviço Dedicada (*Machine User*)

Quando a organização opta por trabalhar apenas com Fine-Grained PATs sem criar GitHub App:

1. Cria-se uma conta GitHub dedicada de automação (ex.: `@sdlc-automation-bot`) adicionada como membro da organização com permissão de escrita/criação de repositórios.
2. O Fine-Grained PAT salvo em `sdlc-github-org-pat` é gerado logado na conta `@sdlc-automation-bot`.
3. O arquivo `.github/CODEOWNERS` lista apenas os aprovadores humanos (`* @ernani` ou `* @<org>/arquitetura-cloud`).
4. **Resultado:** `@sdlc-automation-bot` é o criador do PR e qualquer humano listado em `CODEOWNERS` aprova normalmente com `required_approving_review_count: 1`.

---

## Modelo 3 (Ativo por Padrão em Sandbox Solo-Owner): PR Obrigatório + Gate Determinístico Criptográfico (`0` Reviews Bloqueantes)

Em ambientes de demonstração/sandbox onde há **um único dono humano** (`@ernani`) usando seu próprio Fine-Grained PAT (sem conta secundária e sem GitHub App):

- A Branch Protection da `main` em `cloud-provisioning-approvals` é configurada com:
  - **Pull Request obrigatório** (`required_pull_request_reviews` ativo com `required_approving_review_count: 0`, vedando commits acidentais sem PR);
  - **Status Check obrigatório (`strict: true`)**: `Enforce Mandatory Budget, TTL & GCP_DEPLOYED_VERSION SHA Lock` (valida orçamento, TTL, tag imutável `GCP_DEPLOYED_VERSION`, commit SHA anti-tampering e assinatura criptográfica `HMAC-SHA256`);
  - **Bloqueio de escrita destrutiva**: `allow_force_pushes: false` e `allow_deletions: false`;
  - **Execução de deploy restrita via Keyless OIDC WIF**: somente merges reais em `refs/heads/main` de `cloud-provisioning-approvals` obtêm token STS no GCP (`sdlc-provisioner-sa`).
- **Resultado:** assim que o check determinístico fica verde, o dono do repositório clica diretamente em **Merge pull request** para autorizar o provisionamento, sem *deadlock* de auto-aprovação e sem precisar remover a proteção da branch `main`.
