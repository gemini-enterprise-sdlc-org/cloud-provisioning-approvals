# Php Bulletin Board

> React 18 + TypeScript + Vite 5 Static SPA + PHP 8.3 (Laravel-style MVC + PDO PostgreSQL)

## Stack Poliglota Homologada
- **Frontend / Mobile**: `React 18 + TypeScript + Vite 5 Static SPA`
- **Backend / Runtime**: `PHP 8.3 (Laravel-style MVC + PDO PostgreSQL)`
- **Banco de Dados**: Cloud SQL PostgreSQL 16 (`db_php_bulletin_board`, schema `php_bulletin_board`)

## Telas & Componentes
- Tela Principal (Php Bulletin Board)

## Controles Interativos
- Interação em tempo real conectada à API /api/*

## Endpoints da API
- `GET /api/health`
- `GET /api/status`


# Requisitos & Histórias Gherkin — Php Bulletin Board

## Demanda Original
Criar um Bulletin Board / Fórum Comunitário moderno em PHP 8.3 com arquitetura Laravel MVC (composer.json, routes/api.php, app/Http/Controllers, public/index.php) e frontend React 18 + Vite 5, conectado ao Cloud SQL PostgreSQL (schema 'bulletin_board'). Deve ter filtro por categorias ('Avisos', 'Engenharia', 'Cloud Architecture', 'Dúvidas'), tópicos fixados, listagem de threads com contagem de respostas, visualização detalhada do tópico, formulário para abrir Novo Tópico (POST /api/threads) e formulário para publicar Respostas no tópico (POST /api/threads/:id/replies), além de GET / e GET /api/health e GET /api/status retornando HTTP 200 JSON. Pré-carregar 3 tópicos iniciais ricos no PostgreSQL.

## Requisitos Funcionais
- Interação em tempo real conectada à API /api/*

## Critérios de Aceite (Gherkin)
```gherkin
Funcionalidade: Php Bulletin Board
  Cenário: Uso interativo ponta a ponta com persistência PostgreSQL
    Dado que o usuário acessa o frontend estático via HTTPS GCLB (.nip.io)
    Quando executa uma ação na interface conectada a /api/*
    Então o backend PHP 8.3 (Laravel-style MVC + PDO PostgreSQL) processa e persiste no schema php_bulletin_board
```