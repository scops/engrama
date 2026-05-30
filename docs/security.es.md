# Política de seguridad

## Versiones soportadas

Engrama se encuentra en desarrollo activo pre-1.0. Las correcciones de
seguridad se publican en `main` y se lanzan como nueva versión minor.
Las versiones minor anteriores no reciben backports salvo que se
indique explícitamente en las notas del release.

| Versión | Soporte            |
| ------- | ------------------ |
| 0.13.x  | :white_check_mark: |
| < 0.13  | :x:                |

## Reportar una vulnerabilidad

**Por favor, no abráis un issue, pull request ni discusión pública para
vulnerabilidades de seguridad.** Eso expone el fallo antes de que haya
una corrección lista.

Usad en su lugar el reporte privado de vulnerabilidades de GitHub:

1. Abrid <https://github.com/scops/engrama/security/advisories/new>.
2. Rellenad un advisory privado con:
   - una descripción breve y el impacto,
   - pasos para reproducir (idealmente un script o comando mínimo),
   - la versión de Engrama afectada, versión de Python y sistema operativo,
   - qué backend estaba activo (SQLite o Neo4j),
   - cualquier payload de prueba de concepto o datos de ejemplo que hayáis usado.

Podéis esperar un acuse de recibo en un plazo de cinco días laborables y
una actualización de estado en diez. Si el reporte es válido,
acordaremos un calendario de divulgación antes de cualquier publicación,
y os acreditaremos en el CHANGELOG si lo deseáis.

## Alcance

Dentro del alcance:

- El paquete `engrama` y sus CLIs (`engrama`, `engrama-mcp`).
- Los backends de almacenamiento SQLite y Neo4j distribuidos con este repositorio.
- El adaptador MCP, el SDK de Python y la capa de proveedores de embeddings.
- Los archivos de configuración por defecto (`profiles/`, `.env.example`) y el
  pipeline de build / release en `.github/workflows/`.

Fuera del alcance (reportad directamente upstream):

- Vulnerabilidades en servicios de terceros con los que Engrama puede
  comunicarse — el servidor Neo4j, Ollama, OpenAI, LM Studio, vLLM,
  llama.cpp, Jina, etc.
- Problemas que ya requieren ejecución de código en el host, acceso de
  escritura a `~/.engrama/` o credenciales de API comprometidas.
- Hallazgos en forks o redistribuciones derivadas; contactad
  directamente con los mantenedores de esos proyectos.

## Aislamiento por tenant (multi-tenant)

Desde **0.13.0** cada nodo y cada relación pertenece a una identidad
`(org_id, user_id)`, y las lecturas son **fail-closed** (Spec 001). Este es
el modelo de aislamiento que hay que entender antes de exponer Engrama a más
de un usuario.

- **La identidad es obligatoria en las escrituras.** `engrama_remember` /
  `engrama_relate` sellan `(org_id, user_id)` en el nodo o la arista. Una
  escritura que no puede resolver una identidad completa se rechaza, nunca se
  almacena sin scope.
- **Las lecturas no devuelven nada sin un scope completo.** Los helpers de
  scope (`scope_filter_cypher` / `scope_filter_sql`) emiten `(false)` /
  `(1 = 0)` para un scope `None`, vacío o resuelto a medias. Una lectura que
  llega a ellos sin un `(org_id, user_id)` completo devuelve **cero filas** —
  nunca se ensancha a "verlo todo". No hay ruta admin "see-all" a través de
  los helpers.
- **Engrama no autentica.** Consume una identidad ya aseverada upstream. En
  una instalación de un solo proceso no hay gateway ni cabeceras, así que
  corre como una **identidad standalone** estable (derivada una vez al
  arranque) y todas las lecturas/escrituras la comparten — el aislamiento es
  un no-op, pero se ejercita el mismo camino de código. En un despliegue
  multi-tenant, un gateway delante fija `X-Engrama-Org-Id` /
  `X-Engrama-User-Id` por petición; exactamente una cabecera presente resuelve
  a cero resultados, nunca a un error explotable.
- **Defensa en profundidad, tres capas:** el resolver por petición en el
  límite MCP (rechaza cabeceras parciales), el guard de escritura del motor
  (lanza ante una llamada SDK directa sin scope completo) y un guard de CI
  (`scripts/check_scoped_queries.py`) que rompe el build ante cualquier query
  de backend nueva que esquive el helper de scope sin un
  `# scope-exempt: <razón>` explícito.
- **Migrar un grafo existente.** Un grafo pre-0.13 no tiene identidad en sus
  filas, así que bajo lecturas fail-closed esas filas son invisibles. Ejecutad
  `engrama migrate tenancy --dry-run` para previsualizar y luego
  `engrama migrate tenancy --owner-sub <sub> --apply` para sellar la propiedad
  y restaurar la visibilidad.

### Herramientas admin / cross-tenant

Dos herramientas **no** están aisladas por tenant por diseño y un gateway
multi-tenant debería caparlas para que un tenant normal no las alcance:

- `engrama_status` — introspección en runtime; sus conteos son a nivel de
  **todo el deployment** y no exige identidad.
- `engrama_reindex` — su escaneo de candidatos está acotado al tenant
  llamante (no filtra datos ajenos), pero es un re-embed masivo de carácter
  admin; un gateway puede caparla igualmente por coste/abuso.

`engrama_status` lista ambas en un campo `admin_tools` de su propia respuesta,
para que un gateway descubra qué capar en runtime sin hardcodear nombres.
Engrama OSS solo **declara** este límite; aplicarlo (y toda la autenticación)
es trabajo del gateway.

## Notas de endurecimiento para operadores

Algunos valores por defecto que conviene conocer al desplegar Engrama:

- `~/.engrama/engrama.db` es SQLite plano. Tratadlo como cualquier otra
  base de datos de aplicación: mantenedlo fuera de sistemas de archivos
  compartidos, haced copias de seguridad y confiad en los permisos del
  sistema de archivos para la protección en reposo.
- Los proveedores de embeddings accesibles vía `OPENAI_BASE_URL` deben
  usar HTTPS salvo que el endpoint esté en localhost o en una red de
  confianza.
- El adaptador MCP está diseñado para comunicarse con un cliente local
  (Claude Desktop, un SDK, etc.). No está endurecido para exposición
  directa en internet público — poned vuestro propio gateway autenticado
  delante si necesitáis acceso remoto.
- El **transporte Streamable HTTP** opcional (`ENGRAMA_TRANSPORT=http`) se
  publica **sin autenticación**. Enlazado a su dirección loopback por
  defecto (`127.0.0.1`) tiene la misma superficie de ataque que stdio —
  solo los procesos locales pueden alcanzarlo. Enlazarlo fuera de loopback
  (`0.0.0.0`, una IP de LAN, un proxy inverso o un túnel) lo convierte en
  un endpoint de lectura/escritura sin autenticar sobre todo el grafo de
  memoria: no lo hagáis hasta que llegue OAuth. La validación integrada de
  `Origin`/`Host` protege frente a DNS-rebinding desde un navegador local.
  Consultad la [guía de Streamable HTTP](saas/streamable-http.md).
