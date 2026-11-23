# PRD — `nodejs-todo-board` (Lista de Tarefas de Engenharia)

## 1. Visão do Produto & Contexto de Negócio

- **Aplicação**: **Lista de Tarefas de Engenharia** (`nodejs-todo-board`)
- **Subtítulo Funcional**: Acompanhamento de tarefas técnicas com filtros, KPIs e persistência em PostgreSQL.
- **Demanda / Justificativa**: Criar um aplicativo Node.js 22 + React 18 + TypeScript + PostgreSQL de Lista de Tarefas (Todo App) para times de engenharia, com KPIs de tarefas pendentes/em andamento/concluídas, busca, filtro por prioridade e status, criação rápida de nova tarefa, checkbox de conclusão e persistência no PostgreSQL.
- **Entidade Principal de Domínio**: `Tarefa` / `Tarefas`
- **Isolamento de Dados**: Cloud SQL PostgreSQL compartilhado (`sdlc-shared-pgsql`), database `db_nodejs_todo_board`, schema exclusivo `nodejs_todo_board` (zero uso de `public`).

## 2. Regras de Negócio Numeradas

- **RN-001**: Persistir tarefas no PostgreSQL utilizando o schema nodejs_todo_board via API REST em Node.js 22.
- **RN-002**: Disponibilizar busca textual e filtros por prioridade (ALTA, MEDIA, BAIXA) e status (pendente, em_andamento, concluida).
- **RN-003**: Retornar contadores de KPIs para tarefas pendentes, em andamento e concluídas.
- **RN-004**: Permitir criação rápida de tarefas com validação dos campos obrigatórios e atualização direta do checkbox de conclusão sincronizado com o status concluida.
- **RN-005 [LGPD & GOVERNANÇA]**: Nenhum dado pessoal sensível sem mascaramento em telas ou logs; autenticação e borda protegidas por GCLB HTTPS + Cloud Armor WAF.

## 3. Histórias de Usuário em Formato Gherkin (INVEST)

### US-001 — Consulta Operacional com Indicadores em Tempo Real e Filtros
```gherkin
Funcionalidade: Painel de Gestão de Tarefas
  Cenário: Carregamento inicial da tela com indicadores e registros do PostgreSQL
    Dado que o operador acessa o portal "Lista de Tarefas de Engenharia" via HTTPS (.nip.io)
    Quando a aplicação consulta GET /api/items e GET /api/status na mesma origem
    Então a interface exibe os 4 indicadores KPI consolidados e os 6 registros iniciais do schema "nodejs_todo_board"
    E permite filtrar por busca textual, status (`PENDENTE` (Pendente), `EM_ANDAMENTO` (Em andamento), `CONCLUIDA` (Concluída)), prioridade e categoria
```

### US-002 — Cadastro Validado de Tarefa com Persistência PostgreSQL
```gherkin
Funcionalidade: Criação de Tarefa
  Cenário: Submissão válida pelo formulário modal da aplicação
    Dado que o operador clica em "+ Novo(a) Tarefa" e preenche os campos de domínio
    Quando envia POST /api/items com título, categoria, prioridade e metadados estruturados
    Então a API valida as invariantes de negócio, gera protocolo "TASK-XXXX", persiste em "nodejs_todo_board.registros" e retorna HTTP 201
```

### US-003 — Conclusão em 1 Clique, Mudança de Status e Exclusão Auditada
```gherkin
Funcionalidade: Ciclo de Vida de Tarefa
  Cenário: Atualização de status ou conclusão rápida diretamente na listagem
    Dado um registro existente exibido na visão Tabela ou Cards
    Quando o operador alterna o status ou marca o item como concluído (PATCH /api/items/:id)
    Então o estado é gravado imediatamente no Cloud SQL PostgreSQL e os KPIs do topo são recalculados
```

## 4. Especificação de Telas e Componentes Frontend (React 18 + TypeScript + Vite)

- **Faixa Institucional Fixa**: Banner superior `"Ambiente Governado Enterprise Sandbox (ernani-demo-argolis)"` persistente em todas as visualizações.
- **Cabeçalho Executivo**: Título `Lista de Tarefas de Engenharia`, badge do schema `nodejs_todo_board`, status da conexão PostgreSQL e botão primário **"+ Novo(a) Tarefa"**.
- **Faixa de 4 KPIs**: Total de Tarefas, Pendentes, Em Andamento e Concluídos (com destaque para itens de Alta Prioridade).
- **Barra de Filtros e Visualização**: Busca instantânea, pílulas de status, seletores de prioridade/categoria e alternância entre **Tabela** e **Cards**.

### Campos Específicos do Domínio (`detalhes` JSONB)

| Chave | Rótulo na UI | Tipo | Obrigatório |
|---|---|---|---|
| `descricao` | Descrição | `textarea` | Não |
| `repositorio` | Repositório | `text` | Não |
| `tipo_tarefa` | Tipo | `select` | Sim |

## 5. Contrato de Endpoints REST da API (Node.js 22 + Cloud Run v2)

| Método & Rota | Finalidade | Resposta Principal |
|---|---|---|
| `GET /api/health` · `GET /api/status` | Saúde do container, modo `postgresql`, database `db_nodejs_todo_board` e schema `nodejs_todo_board` | `200 OK` |
| `GET /api/spec` | Contrato completo de UI, status, categorias e campos customizados | `200 OK` |
| `GET /api/items` | Lista filtrada (`?q=&status=&prioridade=&categoria=`) + objeto `kpis` | `200 OK` |
| `POST /api/items` | Valida e insere novo registro em `nodejs_todo_board.registros` | `201 Created` |
| `PATCH /api/items/:id` | Atualiza status, flag `concluido` ou campos do registro | `200 OK` |
| `DELETE /api/items/:id` | Remove registro por UUID | `200 OK` |

## 6. Esquema PostgreSQL & Massa Inicial (`seed` de 6 registros)

Tabela `nodejs_todo_board.registros` inicializada automaticamente no boot da API via `sql/001_init_and_seed.sql`:

| Protocolo | Título Inicial | Status | Prioridade | Categoria | Responsável |
|---|---|---|---|---|---|
| `TASK-1001` | Configurar conexão PostgreSQL no servidor Node.js 22 | `CONCLUIDA` | `ALTA` | Banco de Dados | Equipe |
| `TASK-1002` | Criar endpoints REST para CRUD de tarefas | `CONCLUIDA` | `ALTA` | Backend | Equipe |
| `TASK-1003` | Implementar cards de KPIs por status | `EM_ANDAMENTO` | `MEDIA` | Frontend | Equipe |
| `TASK-1004` | Adicionar busca e filtros por status e prioridade | `EM_ANDAMENTO` | `MEDIA` | Frontend | Equipe |
| `TASK-1005` | Corrigir sincronização do checkbox de conclusão | `PENDENTE` | `ALTA` | Frontend | Não atribuído |
| `TASK-1006` | Criar testes de integração para filtros da API | `PENDENTE` | `BAIXA` | Testes | Não atribuído |

## 7. Parâmetros de Governança no Sandbox (`ernani-demo-argolis`)

- **Orçamento Máximo (`budget_brl`)**: R$ 150.00
- **Tempo de Vida (`ttl`)**: `1h` (contado após validação do certificado HTTPS gerenciado)
- **Canais de Alerta SRE (`alert_emails`)**: `definido no manifesto de aprovação`