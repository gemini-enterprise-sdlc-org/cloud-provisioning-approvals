# SciCalc Studio (`dotnet-react-calc`)

Calculadora Científica e Padrão completa com backend em **.NET 8 Minimal API (C# + Npgsql)** conectado ao **Cloud SQL PostgreSQL** (schema dedicado `dotnet_react_calc`) e frontend em **React 18 + TypeScript + Vite 5**.

## Arquitetura

- **Backend (`apps/api/`)**:
  - `Api.csproj` (`net8.0`, `Npgsql 8.0.5`)
  - `MathExpressionParser.cs`: parser matemático recursivo descendente seguro (sem código dinâmico), com suporte a `DEG`/`RAD`, trigonometria (`sin`, `cos`, `tan`, `asin`, `acos`, `atan`), logaritmos (`ln`, `log`), raízes (`sqrt`, `cbrt`), potência (`^`), fatorial (`n!`), constantes (`pi`, `e`), parênteses e multiplicação implícita.
  - `CalculationRepository.cs`: persistência parametrizada via `Npgsql` sobre Unix Socket do Cloud SQL (`CLOUDSQL_SOCKET`, `DB_NAME`, `PRIMARY_SCHEMA`, `PG_SUPER_PASS`), com criação automática de schema/tabela + seed idempotente e fallback automático em memória quando executada localmente sem socket.
  - Endpoints:
    - `GET /`
    - `GET /api/health`
    - `GET /api/status`
    - `POST /api/calculate`
    - `GET /api/history`
    - `DELETE /api/history`
    - `DELETE /api/history/{id}`
- **Frontend (`apps/web/`)**:
  - SPA estática React 18 + TypeScript + Vite 5 compilada para `apps/web/dist/`.
  - Visor duplo escuro/moderno (expressão simbólica + pré-visualização instantânea / resultado), alternador `DEG`/`RAD`, alternador `Científica`/`Padrão`, memória (`MC`, `MR`, `M+`, `M-`), teclado físico e painel lateral **Fita de Histórico** sincronizado com PostgreSQL.
- **Banco de Dados (`sql/001_init_and_seed.sql`)**:
  - Schema dedicado `dotnet_react_calc` e tabela `dotnet_react_calc.calculations`.

## Desenvolvimento Local

```bash
# Backend (.NET 8)
cd apps/api
dotnet run

# Frontend (React + Vite)
cd apps/web
npm install
npm run dev
```


# Requisitos & Histórias Gherkin — SciCalc Studio — Calculadora Científica & Padrão

## Demanda Original
Criar uma Calculadora Científica e Padrão completa com backend em .NET Core 8 Minimal API (C# + Npgsql no Cloud SQL PostgreSQL, schema 'dotnet_calc') e frontend em React 18 + TypeScript + Vite 5. A interface deve ter visor duplo escuro/moderno (expressão simbólica + resultado), alternador DEG/RAD, botões científicos (sin, cos, tan, sqrt, ln, log, x^y, n!, pi, e, parênteses) e aritméticos (+, -, *, /, %), suporte ao teclado físico, parser matemático recursivo seguro SEM eval(), gravação de cada cálculo via POST /api/calculate no PostgreSQL e painel lateral 'Fita de Histórico' (GET /api/history, DELETE /api/history), além de GET /, GET /api/health e GET /api/status retornando HTTP 200 JSON.

## Requisitos Funcionais
- Alternador DEG/RAD e Modo Científica/Padrão
- Botões científicos (sin, cos, tan, asin, acos, atan, sqrt, ln, log, x^y, x², 1/x, n!, π, e, parênteses, Ans)
- Operações aritméticas (+, -, ×, ÷, %, ±) e teclas de memória (MC, MR, M+, M-)
- Suporte completo ao teclado físico
- Fita de Histórico com busca, recarga de expressão, exclusão individual e limpeza total

## Critérios de Aceite (Gherkin)
```gherkin
Funcionalidade: SciCalc Studio — Calculadora Científica & Padrão
  Cenário: Uso interativo ponta a ponta com persistência PostgreSQL
    Dado que o usuário acessa o frontend estático via HTTPS GCLB (.nip.io)
    Quando executa uma ação na interface conectada a /api/*
    Então o backend dotnet-csharp (.NET 8 Minimal API + Npgsql 8.0.5) processa e persiste no schema dotnet_react_calc
```