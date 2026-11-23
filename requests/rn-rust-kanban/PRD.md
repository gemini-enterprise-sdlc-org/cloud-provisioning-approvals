# RN Rust Kanban (`rn-rust-kanban`)

Multiplatform Kanban project tracker built with **React Native** (`mobile/src/KanbanBoard.tsx`), compiled to Web via **React Native Web + Vite 5** (`apps/web/`), and backed by a **Rust 1.83 API (Axum 0.7 + Tokio + tokio-postgres)** (`apps/api/`) persisting to **Cloud SQL PostgreSQL** under dedicated schema `rn_rust_kanban`.

## Architecture
- `mobile/src/KanbanBoard.tsx`: Genuine React Native primitives (`View`, `Text`, `Pressable`, `TextInput`, `ScrollView`, `StyleSheet`) implementing a 4-column Kanban board (`Backlog`, `In Progress`, `Review`, `Done`), project filter pills, column counters, priority badges (`High`, `Medium`, `Low`), assignee chips, card creation form, and one-tap stage transitions.
- `apps/web/`: Vite 5 + `react-native-web` alias compiling `mobile/src/KanbanBoard.tsx` into static assets (`apps/web/dist/`).
- `apps/api/`: Rust Axum 0.7 API serving `GET /`, `GET /api/health`, `GET /api/status`, `GET /api/cards`, `POST /api/cards`, `POST /api/cards/:id/move`, `PATCH /api/cards/:id`, and `DELETE /api/cards/:id`.
- `sql/001_init_and_seed.sql`: Idempotent PostgreSQL schema (`rn_rust_kanban`), `cards` table, append-only `trilha_auditoria` table, and 4 seed cards.

## Local Development
1. Run Rust API:
   ```bash
   cd apps/api && cargo run
   ```
2. Run Web companion (Vite 5 + React Native Web):
   ```bash
   cd apps/web && npm install && npm run dev
   ```
3. Build static web bundle:
   ```bash
   cd apps/web && npm run build
   ```


# Requisitos & Histórias Gherkin — RN Rust Kanban

## Demanda Original
Criar um aplicativo mobile React Native (em mobile/src/KanbanBoard.tsx usando View, Text, Pressable, TextInput, ScrollView e StyleSheet de 'react-native', compilado também para web em apps/web/ via react-native-web + Vite 5) com backend em Rust API (Axum 0.7 + Tokio + tokio-postgres no Cloud SQL PostgreSQL, schema 'rust_kanban') para controlar projetos num Board Kanban em 4 colunas ('Backlog', 'In Progress', 'Review', 'Done'). Deve exibir filtro por projeto, contadores por coluna, cards com prioridade ('High', 'Medium', 'Low') e responsável, formulário para criar novo card (POST /api/cards), botões nos cards para avançar/voltar coluna (POST /api/cards/:id/move ou PATCH /api/cards/:id) persistindo no PostgreSQL, 4 cards iniciais de seed, e rotas GET /, GET /api/health e GET /api/status retornando HTTP 200 JSON.

## Requisitos Funcionais
- Project filter pills (Todos os Projetos + projetos dinamicos)
- New card composer (title, description, project, priority High/Medium/Low, assignee, initial column)
- One-tap card stage transition buttons (Voltar <- / Avancar ->)
- Live refresh and API/PostgreSQL health indicator

## Critérios de Aceite (Gherkin)
```gherkin
Funcionalidade: RN Rust Kanban
  Cenário: Uso interativo ponta a ponta com persistência PostgreSQL
    Dado que o usuário acessa o frontend estático via HTTPS GCLB (.nip.io)
    Quando executa uma ação na interface conectada a /api/*
    Então o backend Rust 1.83 (Axum 0.7 + Tokio + tokio-postgres) processa e persiste no schema rn_rust_kanban
```