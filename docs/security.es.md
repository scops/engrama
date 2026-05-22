# Política de seguridad

## Versiones soportadas

Engrama se encuentra en desarrollo activo pre-1.0. Las correcciones de
seguridad se publican en `main` y se lanzan como nueva versión minor.
Las versiones minor anteriores no reciben backports salvo que se
indique explícitamente en las notas del release.

| Versión | Soporte            |
| ------- | ------------------ |
| 0.9.x   | :white_check_mark: |
| < 0.9   | :x:                |

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
