# Visión

## El problema

Los agentes de IA tienen memoria a corto plazo. Cada conversación empieza de cero. Los sistemas de memoria existentes son o demasiado simples (archivos JSON planos), o demasiado complejos (pipelines de embeddings, bases de datos vectoriales, orquestadores en la nube), o comprometen la privacidad al depender de servicios externos.

Karpathy construyó su segundo cerebro en Markdown y wikis — eso funciona para humanos porque los humanos realizan búsqueda semántica natural al leer. Los agentes no. Los agentes necesitan **grafos**: relaciones explícitas, recorridos eficientes, consultas precisas sin escanear cada documento.

## La solución

**Engrama** es un framework Python plug-and-play que proporciona a cualquier agente de IA una memoria persistente y estructurada respaldada por un grafo de conocimiento. El agente puede recordar, asociar, olvidar y razonar sobre el conocimiento acumulado — exactamente como lo haría un humano con buena memoria.

El grafo funciona sobre **SQLite + sqlite-vec** (por defecto desde la 0.9 — un único archivo, sin servicios externos, `git clone` + `uv sync` y a correr; Engrama aún no está en PyPI) o **Neo4j 5.26 LTS** (opcional para producción multiproceso, índices vectoriales grandes o equipos que ya usan Cypher). Ambos exponen el mismo modelo de datos y las mismas doce herramientas MCP — consulta [backends.md](backends.md) para la guía de elección.

## Qué lo diferencia

| Comparado con | Diferencia |
|---|---|
| MCP Memory (JSON) | Engrama escala. Un archivo JSON de 10 000 entidades es inmanejable. Un grafo de 10 000 nodos se recorre en milisegundos. |
| Obsidian / Markdown | Obsidian es para humanos. Engrama es para agentes. Las relaciones son ciudadanos de primera clase, no enlaces de texto. |
| Mem0 / Zep (cloud) | Engrama es local-first. Tus datos nunca salen de tu máquina. |
| RAG + base vectorial | Engrama no necesita embeddings para consultas estructuradas. Son una capa opcional, no un requisito. |
| Otros frameworks de memoria con grafos | Cero servicios externos en la instalación por defecto. Ni Docker, ni JVM, ni nube — `git clone` + `uv sync` (publicación en PyPI prevista). |

## Para quién es

- **Desarrolladores de agentes** que quieren memoria persistente en 5 minutos
- **Investigadores** que quieren un grafo de conocimiento personal
- **Formadores y educadores** que quieren que su agente recuerde proyectos, alumnos y decisiones pedagógicas
- **Cualquiera que trabaje con LLMs** y esté cansado de repetir contexto en cada sesión

## Filosofía de diseño

1. **Local-first** — tu grafo, en tu máquina, cero dependencias de la nube
2. **Instalación sin fricción** — `git clone … && uv sync && uv run engrama init`, sin Docker, sin JVM, sin servicios que configurar (Neo4j está ahí cuando lo necesites — consulta [backends.md](backends.md))
3. **Agnóstico del agente** — funciona con Claude, LangChain, n8n o cualquier cosa que hable MCP o Python
4. **Agnóstico del backend** — skills, herramientas y motor hablan con los protocolos abstractos `GraphStore` / `VectorStore` / `EmbeddingProvider`. Cambiar de SQLite a Neo4j (o, en el futuro, Chroma / pgvector / ArcadeDB) es cambiar una sola variable
5. **Esquema como configuración** — los perfiles definen tipos de nodos y relaciones sin tocar código
6. **Grafos primero, vectores después** — la estructura explícita siempre supera a la búsqueda semántica por fuerza bruta en eficiencia

## El nombre

*Engrama* (del griego *engramma*): la huella física que deja un recuerdo en el tejido neural. La marca que persiste. Exactamente lo que este framework hace por los agentes.
