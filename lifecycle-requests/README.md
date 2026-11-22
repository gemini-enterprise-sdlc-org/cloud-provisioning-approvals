# Pedidos de override de ciclo de vida

Arquivos `*.json` nesta pasta são gerados **somente** pelo agente ADK (`request_workload_lifecycle_override`) a pedido de
um aprovador autorizado de EA/PE, com selo HMAC-SHA256. O workflow `workload-lifecycle-override.yml` processa cada pedido e o move para
`processed/` com o resultado. Pedidos sem selo válido, de quem não é aprovador ou para ambientes inativos são rejeitados.
