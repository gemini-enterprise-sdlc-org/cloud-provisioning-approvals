# PRD — Slack Clone (`slack-clone`)

## 1. Objetivo
Workspace colaborativo de comunicação em tempo real inspirado no Slack, com canais públicos (`#`), canais privados (cadeado), mensagens diretas (DMs 1:1), threads em painel lateral retrátil, reações com emoji em tempo real, busca global (`Cmd/Ctrl+K`) e indicadores de presença/digitação.

## 2. Frontend (SPA React + TypeScript + Vite + Tailwind CSS + Lucide Icons)
- **Layout Split-Pane 3 Colunas** (tema escuro/claro aubergine):
  - **Workspace Sidebar**: seletor rápido de persona/usuário ativo com status de presença online/ausente, botão de busca rápida (`Cmd/Ctrl+K`), lista expansível de Canais Públicos (`#`) e Privados (cadeado) com badges de mensagens não lidas e botão `+`, e seção de DMs com status online.
  - **Timeline Principal**: cabeçalho fixo com nome, tópico e membros; mensagens agrupadas por autor e janela de tempo com divisores de data; formatação Markdown segura (negrito, itálico, código inline, blocos de código); edição e soft-delete pelo autor com selo `(editado)`; pills clicáveis de reações com emoji (toggle + picker); resumo clicável de threads (`N respostas`); compositor com atalhos de formatação e indicador de digitação.
  - **Thread Drawer (Gaveta Lateral Direita)**: mensagem raiz fixada, respostas em tempo real, reações por resposta e opção "Também enviar no canal".
  - **Modais**: Busca Global (`Cmd/Ctrl+K`) com filtros por canal e autor, Novo Canal (público/privado), Nova DM e Gerenciamento de Membros.

## 3. Backend (Node.js 22 LTS + TypeScript + Fastify + @fastify/websocket)
- Endpoints REST: `GET /api/health`, `GET /api/status`, `GET /api/bootstrap`, `GET/POST /api/channels`, `POST /api/channels/:id/join`, `POST /api/channels/:id/members`, `POST /api/dms`, `GET/POST /api/conversations/:id/messages`, `PATCH/DELETE /api/messages/:id`, `GET/POST /api/messages/:id/thread`, `POST /api/messages/:id/reactions/toggle`, `POST /api/conversations/:id/read`, `GET /api/search?q=&conversationId=&authorId=`.
- WebSocket `WS /api/ws`: eventos `message.created`, `message.updated`, `message.deleted`, `reaction.toggled`, `thread.replied`, `typing.indicator`, `presence.updated`.
- Segurança: isolamento anti-IDOR via `conversation_members` em canais privados e DMs (REST e WebSocket), sanitização XSS em Markdown, zero `localStorage` para tokens, zero `innerHTML`/`eval`.

## 4. Persistência (Cloud SQL PostgreSQL 16 — `db_slack_clone`, schema dedicado `slack_clone`)
- Tabelas: `slack_clone.users`, `slack_clone.conversations`, `slack_clone.conversation_members`, `slack_clone.messages` (com `search_tsv TSVECTOR` + índice GIN em português) e `slack_clone.message_reactions` (`UNIQUE (message_id, user_id, emoji)`).
- Seed idempotente: 5 usuários (Ana Silva, Bruno Costa, Carla Mendes, Diego Rocha, Elena Souza), 4 canais públicos (`#geral`, `#engenharia`, `#produto`, `#aleatorio`), 1 canal privado (`#lideranca-tech`), 2 DMs ativas, ~25 mensagens com código, 3 threads e reações com emoji.