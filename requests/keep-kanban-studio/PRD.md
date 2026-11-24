# Keep & Kanban Creative Studio (`keep-kanban-studio`)

Aplicação full-stack criativa combinando **Post-It Notes (estilo Google Keep)**, **Quadro Kanban sincronizado** e **Espaço 3D interativo em WebGL (Three.js)** sobre **Material Design 3**, com backend REST em **Node.js 22 + TypeScript** conectado ao **Cloud SQL PostgreSQL 16** (schema dedicado `keep_kanban_studio`).

## Arquitetura

- **Frontend (`apps/web/`)**: Svelte 5 + TypeScript + Vite 5 + Three.js + Material Design 3 (`--md-sys-*` Light/Dark).
  - `base: "./"` para compatibilidade portátil entre `/ui-staging/{slug}/v{N}/` e produção GCLB (`/`).
  - Base de API dinâmica via `window.__SDLC_API_BASE__ || "/api"`.
- **Backend (`apps/api/`)**: Node.js 22 LTS + TypeScript + `pg` sobre Unix Socket do Cloud SQL (`CLOUDSQL_SOCKET`, `DB_NAME`, `PRIMARY_SCHEMA=keep_kanban_studio`, `PG_SUPER_PASS`) com inicialização idempotente de schema, tabela e seed realista.
- **Banco de Dados (`sql/001_init_and_seed.sql`)**: Schema dedicado `keep_kanban_studio`, tabela `notes` e trilha de auditoria *append-only* `trilha_auditoria`.

## Endpoints REST

- `GET /api/health`
- `GET /api/status`
- `GET /api/notes`
- `POST /api/notes`
- `PATCH /api/notes/:id`
- `DELETE /api/notes/:id`
- `PATCH /api/notes/:id/toggle-pin`
- `PATCH /api/notes/:id/move`

## Build Local

```bash
# Backend
cd apps/api && npm install && npm run build && npm start

# Frontend
cd apps/web && npm install && npm run build
```


# Requisitos & Histórias Gherkin — Keep & Kanban Creative Studio

## Demanda Original
Aplicação 'Keep & Kanban Creative Studio' com frontend criativo promovido 1:1 do UI Staging v2 (Svelte 5 + Three.js WebGL 3D + Material Design 3 com Post-It notes coloridos, fixação no topo e quadro Kanban sincronizado) e backend REST API em Node.js 22 + TypeScript conectado ao Cloud SQL PostgreSQL 16 (schema dedicado keep_kanban_studio) expondo GET /api/health, GET /api/status, GET /api/notes, POST /api/notes, PATCH /api/notes/:id, DELETE /api/notes/:id, PATCH /api/notes/:id/toggle-pin e PATCH /api/notes/:id/move com campos {id, title, content, color, pinned, column, tags, created_at, updated_at} e seed inicial realista.

[PROMOTED FROM APPROVED UI STAGING v2]: Backend API MUST implement these exact REST endpoints expected by the staged frontend: ["GET ${API_BASE}/notes", "POST ${API_BASE}/notes", "PATCH ${API_BASE}/notes/:id", "DELETE ${API_BASE}/notes/:id", "PATCH ${API_BASE}/notes/:id/toggle-pin", "PATCH ${API_BASE}/notes/:id/move"]. UI features: ["Painel de KPIs rápidos em Material Design 3 no topo (Total de Post-Its, Fixados 📌 e Concluídos em Done com barra de progresso)", "Chips clicáveis de filtro rápido por cor de Post-It (Todas as cores + 6 bolinhas coloridas com contador por cor)", "Alternância Dark/Light Mode com tokens Material Design 3 e busca instantânea", "Visão Notas Post-It (Keep) com barra de criação rápida, checklists interativos, seletor de 6 cores, Pin 📌 e grade Masonry responsiva (Fixadas vs Outras)", "Visão Quadro Kanban com 3 colunas (Backlog, Doing, Done), Drag & Drop e botões ← / → sincronizados com PATCH /notes/:id/move", "Visão Espaço 3D (Three.js) em WebGL real com cartões 3D texturizados flutuantes, modos Órbita Suave / Kanban 3D / Esfera Criativa, zoom e inspetor flutuante", "Modal completo de edição de notas e persistência REST dinâmica via window.__SDLC_API_BASE__ com fallback resiliente"].

## Requisitos Funcionais
- Painel de KPIs Material Design 3 (Total, Fixados e Concluídos em Done com barra de progresso)
- Chips clicáveis de filtro rápido por cor (Todas + 6 cores com contador dinâmico)
- Busca instantânea por título, conteúdo e tags + alternância Dark/Light Mode MD3
- Barra expansível de criação rápida de Post-It e itens de checklist
- Checklists clicáveis com persistência imediata via PATCH /api/notes/:id
- Fixar/Desafixar (Pin) sincronizado via PATCH /api/notes/:id/toggle-pin
- Drag & Drop e botões esquerda/direita sincronizados via PATCH /api/notes/:id/move
- Câmera orbital 3D interativa, zoom, troca de layout espacial e seleção por raycasting

## Critérios de Aceite (Gherkin)
```gherkin
Funcionalidade: Keep & Kanban Creative Studio
  Cenário: Uso interativo ponta a ponta com persistência PostgreSQL
    Dado que o usuário acessa o frontend estático via HTTPS GCLB (.nip.io)
    Quando executa uma ação na interface conectada a /api/*
    Então o backend nodejs-typescript processa e persiste no schema keep_kanban_studio
```