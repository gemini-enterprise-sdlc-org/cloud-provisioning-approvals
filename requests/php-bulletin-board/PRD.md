# PHP 8.3 Bulletin Board — Fórum Comunitário Enterprise

Aplicação full-stack pronta para produção no Google Cloud Run v2 + Cloud SQL PostgreSQL (schema dedicado `php_bulletin_board`) + Cloud Storage / Cloud CDN.

## Arquitetura
- **Backend (`apps/api/`)**: PHP 8.3 com estrutura Laravel-style MVC (`composer.json`, `routes/api.php`, `app/Http/Controllers/`, `app/Models/`, `public/index.php`) usando `PDO` PostgreSQL sobre Unix Socket (`CLOUDSQL_SOCKET`) com *prepared statements* estritos, proteção anti-IDOR por `tenant_scope` e trilha `trilha_auditoria` *append-only*.
- **Frontend (`apps/web/`)**: React 18 + TypeScript + Vite 5 SPA estática consumindo `/api/*` na mesma origem.
- **Banco de Dados (`sql/001_init_and_seed.sql`)**: Schema dedicado `php_bulletin_board` (nunca `public`) com tabelas `threads`, `posts` e `trilha_auditoria`, pré-carregado com 3 tópicos iniciais ricos.

## Endpoints da API
- `GET /` — Metadados do serviço (HTTP 200 JSON)
- `GET /api/health` — Probe de saúde Liveness/Readiness (HTTP 200 JSON)
- `GET /api/status` — Status da persistência, schema e métricas da comunidade (HTTP 200 JSON)
- `GET /api/threads` — Lista de tópicos com filtros `?category=` e `?q=`, ordenados por fixados e atividade recente
- `GET /api/threads/:id` — Detalhe completo do tópico e suas respostas
- `POST /api/threads` — Criação de novo tópico
- `POST /api/threads/:id/replies` — Publicação de resposta em tópico existente

## Execução Local
```bash
# Backend PHP 8.3 (fallback local seguro quando CLOUDSQL_SOCKET não estiver definido)
cd apps/api
php -S 0.0.0.0:8080 -t public public/index.php

# Frontend React 18 + Vite 5
cd apps/web
npm install
npm run build
```


# Requisitos & Histórias Gherkin — Comunidade Cloud & Engenharia — Bulletin Board

## Demanda Original
Criar um Bulletin Board / Fórum Comunitário moderno em PHP 8.3 com arquitetura Laravel MVC (composer.json, routes/api.php, app/Http/Controllers, public/index.php) e frontend React 18 + Vite 5, conectado ao Cloud SQL PostgreSQL (schema 'bulletin_board'). Deve ter filtro por categorias ('Avisos', 'Engenharia', 'Cloud Architecture', 'Dúvidas'), tópicos fixados, listagem de threads com contagem de respostas, visualização detalhada do tópico, formulário para abrir Novo Tópico (POST /api/threads) e formulário para publicar Respostas no tópico (POST /api/threads/:id/replies), além de GET / e GET /api/health e GET /api/status retornando HTTP 200 JSON. Pré-carregar 3 tópicos iniciais ricos no PostgreSQL.

## Requisitos Funcionais
- Pílulas de filtro por categoria (Todas, Avisos, Engenharia, Cloud Architecture, Dúvidas)
- Campo de busca em tempo real por título, conteúdo ou autor
- Seleção de tópico com visualização detalhada e contagem de respostas
- Formulário para abrir Novo Tópico (POST /api/threads) com opção de fixar
- Formulário inline para publicar Respostas (POST /api/threads/:id/replies)
- Botão de atualização manual e indicador de saúde da API (/api/status)

## Critérios de Aceite (Gherkin)
```gherkin
Funcionalidade: Comunidade Cloud & Engenharia — Bulletin Board
  Cenário: Uso interativo ponta a ponta com persistência PostgreSQL
    Dado que o usuário acessa o frontend estático via HTTPS GCLB (.nip.io)
    Quando executa uma ação na interface conectada a /api/*
    Então o backend php-laravel (PHP 8.3 MVC + PDO PostgreSQL) processa e persiste no schema php_bulletin_board
```