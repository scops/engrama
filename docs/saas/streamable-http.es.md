# Transporte Streamable HTTP

El servidor MCP de Engrama habla dos transportes:

| Transporte | Cuándo | Cómo se selecciona |
|------------|--------|--------------------|
| **stdio** (por defecto) | Clientes de escritorio locales que lanzan el servidor como subproceso (la configuración estándar de Claude Desktop). | `ENGRAMA_TRANSPORT=stdio` (o sin definir). |
| **Streamable HTTP** | Ejecutar Engrama como un servidor HTTP local de larga duración al que te conectas por red. | `ENGRAMA_TRANSPORT=http`. |

El transporte HTTP se apoya en el `MCPServer` del SDK de MCP
(`mcp.server.mcpserver`, SDK v2) — sin dependencia extra. El valor por defecto
sigue siendo `stdio`, así que las configuraciones de Claude Desktop
existentes quedan intactas.

!!! danger "Enlaza solo a loopback — todavía no hay autenticación"
    El transporte HTTP se publica **sin auth**. Ejecútalo enlazado a
    `127.0.0.1` (el valor por defecto) y **nunca** lo expongas en una
    interfaz pública o accesible desde la LAN hasta que llegue la fase de
    OAuth. Consulta [Modelo de seguridad](#security-model).

## Modelo de seguridad { #security-model }

**El HTTP local en loopback tiene la misma superficie de ataque que
stdio.** Con el enlace por defecto (`127.0.0.1`), los únicos procesos que
pueden alcanzar `/mcp` son los que ya se ejecutan en tu máquina —
exactamente la frontera de confianza en la que se apoya stdio (un cliente
local que lanza y habla con un servidor local). Cambiar un cliente local
Claude Desktop / SDK de stdio a HTTP por loopback **no** amplía tu
exposición.

La superficie solo crece si **tú** cambias el despliegue:

- **Enlazar fuera de loopback** — `ENGRAMA_HTTP_HOST=0.0.0.0` o una IP de
  LAN, un proxy inverso, o un túnel tipo SSH / `ngrok` — convierte el
  servidor en un **endpoint remoto sin autenticar**. Cualquiera que
  alcance el puerto puede leer y escribir todo el grafo de memoria. No lo
  hagas, no en esta fase.
- **Una página web local maliciosa** podría intentar dirigir un navegador
  para que haga POST a `http://127.0.0.1:8000/mcp` (un ataque tipo
  DNS-rebinding / CSRF). La [validación de Origin/Host](#origin-validation)
  integrada es la defensa: las peticiones cross-origin se rechazan con
  403, y solo se aceptan valores de `Host` de loopback.

Reglas prácticas para esta fase:

- ✅ Enlace a loopback + cliente local → misma confianza que stdio. Bien.
- ✅ Mantén el `ENGRAMA_ALLOWED_ORIGINS` por defecto (solo loopback).
- ❌ Nada de enlace fuera de loopback, ni exposición pública / LAN, ni
  túneles — salvo que pongas delante tu propio gateway autenticado **y**
  TLS.
- ⏭ La autenticación real (OAuth 2.1) es la siguiente fase; el stub
  `/.well-known/oauth-protected-resource` es su gancho.

## Configuración

Todos los ajustes HTTP son variables de entorno (los flags de CLI las
sobrescriben):

| Variable | Flag CLI | Por defecto | Propósito |
|----------|----------|-------------|-----------|
| `ENGRAMA_TRANSPORT` | `--transport` | `stdio` | `stdio` o `http`. |
| `ENGRAMA_HTTP_HOST` | `--host` | `127.0.0.1` | Dirección de enlace (modo HTTP). |
| `ENGRAMA_HTTP_PORT` | `--port` | `8000` | Puerto TCP (modo HTTP). |
| `ENGRAMA_ALLOWED_ORIGINS` | `--allowed-origins` | solo loopback | CSV de cabeceras `Origin` permitidas. |
| `ENGRAMA_AUTH_ISSUER` | `--auth-issuer` | _(sin definir)_ | Issuer OAuth para el stub RFC 9728. Sin definir → el endpoint devuelve 404. |

El endpoint MCP se sirve en **`/mcp`**.

## Arrancar en modo HTTP (local)

=== "PowerShell"

    ```powershell
    $env:ENGRAMA_TRANSPORT = "http"
    engrama-mcp
    # o, equivalentemente:
    engrama-mcp --transport http --host 127.0.0.1 --port 8000
    ```

=== "bash"

    ```bash
    ENGRAMA_TRANSPORT=http engrama-mcp
    # o:
    engrama-mcp --transport http --host 127.0.0.1 --port 8000
    ```

La selección de backend no cambia respecto al modo stdio (`--backend
sqlite` por defecto, `--backend neo4j` más las variables `NEO4J_*` para
optar por Neo4j).

## Endpoints

| Ruta | Método | Propósito |
|------|--------|-----------|
| `/mcp` | POST/GET | El endpoint MCP Streamable HTTP. |
| `/health` | GET | Sonda de liveness/readiness — 200 si el backend responde, 503 si no. |
| `/.well-known/oauth-protected-resource` | GET | Stub de metadatos RFC 9728 (404 hasta que se defina `ENGRAMA_AUTH_ISSUER`). |

### `/health`

Devuelve `200` con `{"status": "ok", "backend": ..., "node_count": ...}`
cuando el backend configurado responde, y `503` con
`{"status": "error", ...}` cuando no. Útil para sondas de
liveness/readiness de Kubernetes en una futura fase de despliegue.

```bash
curl -i http://127.0.0.1:8000/health
```

Intencionadamente **no** está protegido por la comprobación de Origin
(las sondas no envían cabecera `Origin`). Mantiene una pequeña conexión
cacheada propia — consulta [Versiones del protocolo y sesiones](#session-mode)
para entender por qué las rutas personalizadas no pueden reutilizar el
store del servidor MCP.

### `/.well-known/oauth-protected-resource`

Un stub para la próxima fase de OAuth:

```bash
# Sin issuer configurado → 404
curl -i http://127.0.0.1:8000/.well-known/oauth-protected-resource

# Con un issuer → documento RFC 9728
ENGRAMA_AUTH_ISSUER=https://auth.example.com engrama-mcp --transport http
curl -s http://127.0.0.1:8000/.well-known/oauth-protected-resource
# {"resource": "http://127.0.0.1:8000/mcp",
#  "authorization_servers": ["https://auth.example.com"]}
```

La siguiente fase solo tiene que definir `ENGRAMA_AUTH_ISSUER` (y cablear
un verificador de tokens) — sin cambiar el código de este endpoint.

## Validación de Origin (anti DNS-rebinding) { #origin-validation }

El transporte HTTP usa la protección anti DNS-rebinding integrada en el
SDK de MCP. En cada petición a `/mcp` valida:

- **`Origin`** contra `ENGRAMA_ALLOWED_ORIGINS` — un Origin no permitido se
  rechaza con **403**. Un `Origin` ausente (same-origin / cliente no-navegador
  como `curl`) se permite.
- **`Host`** contra la lista blanca de loopback derivada de `--host`/`--port`
  — un Host que no coincide se rechaza con **421**.

La lista blanca de Origin por defecto es solo loopback, incluyendo
comodines de puerto para que los clientes tipo navegador que se conectan a
`http://localhost:8000` funcionen sin configuración extra:

```
http://localhost, http://127.0.0.1, http://localhost:*, http://127.0.0.1:*
```

Sobrescríbela para un cliente concreto:

```bash
ENGRAMA_ALLOWED_ORIGINS="http://localhost:8000,https://mi-cliente.example" \
  engrama-mcp --transport http
```

Comprobación rápida:

```bash
# Origin no permitido → 403
curl -i -H "Origin: http://evil.com" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -X POST http://127.0.0.1:8000/mcp

# Sin Origin (curl) → pasa la comprobación de seguridad
curl -i -H "Accept: application/json, text/event-stream" \
  http://127.0.0.1:8000/mcp
```

## Versiones del protocolo y sesiones { #session-mode }

El servidor habla las dos eras del protocolo MCP en el mismo endpoint
`/mcp` y elige por petición — no hay nada que configurar:

| Protocolo del cliente | Handshake | Sesión |
|-----------------------|-----------|--------|
| **`2026-07-28`** | Ninguno. Cada petición lleva su versión de protocolo, la info del cliente y sus capacidades en `_meta`; `server/discover` es opcional. | Ninguna — la revisión eliminó `Mcp-Session-Id`. |
| **`2025-11-25`** y anteriores | `initialize` / `notifications/initialized`. | Ninguna por defecto (ver abajo). |

**Sin sesión por defecto.** Los clientes de la era del handshake también
se atienden sin estado (`stateless_http=True`): `initialize` no devuelve
`Mcp-Session-Id` y cada petición es independiente, así que cualquier
réplica puede responder cualquier petición y un reinicio no deja nada que
el cliente tenga que recuperar. Los tools de Engrama son llamadas
petición/respuesta planas — sin **sampling**, **elicitation** ni push del
servidor — así que nada necesita un canal de larga duración. Quien embeba
`create_engrama_mcp()` directamente puede pasar `stateless_http=False` para
volver a dar una sesión por cliente a los clientes de la era del
handshake; los clientes `2026-07-28` siguen sin sesión en cualquier caso.

**La identidad del tenant es por petición.** Las cabeceras
`X-Engrama-Org-Id` / `X-Engrama-User-Id` que inyecta un gateway se leen en
cada petición (Spec 001), así que el aislamiento no depende de una sesión.

**Lifespan.** El lifespan del servidor — que abre el store del grafo, el
vault y el embedder — corre **una vez al arrancar** y lo comparten todas
las peticiones. Un backend caído al arrancar no detiene el servidor:
`/health` responde 503 y los tools devuelven error hasta que vuelve.

Las rutas personalizadas (`/health`) son rutas Starlette normales que
corren fuera de cualquier petición MCP, así que nunca ven el contexto de
lifespan del servidor. Por eso `/health` mantiene su propia conexión de
backend cacheada y creada de forma lazy en vez de acceder al estado de la
petición MCP.

## Conectar clientes

### CLI `mcp` / Inspector (pruebas manuales)

El MCP Inspector o cualquier cliente HTTP de MCP apunta a la URL `/mcp`:

```bash
npx @modelcontextprotocol/inspector
# Transport: "Streamable HTTP"
# URL: http://127.0.0.1:8000/mcp
```

Desde el Inspector puedes listar tools (`engrama_status`, `engrama_search`,
…) y llamarlos para confirmar que el servidor responde de extremo a extremo.

### Claude Desktop (integración personalizada)

Algunas builds de Claude Desktop aceptan un servidor MCP HTTP personalizado;
otras restringen las integraciones personalizadas a HTTPS. Para probarlo:

1. Arranca Engrama en modo HTTP (arriba).
2. En Claude Desktop, añade un servidor / integración MCP personalizada
   apuntando a `http://localhost:8000/mcp`.
3. Confirma que aparecen los tools de Engrama y que `engrama_status`
   devuelve el backend/vault esperados.

**Limitaciones conocidas (esta fase):**

- **Sin auth.** Claude Desktop puede avisar o rechazar una integración
  personalizada sin autenticar.
- **Requisito de HTTPS.** Si tu build exige HTTPS para integraciones
  personalizadas, pon delante del servidor un certificado TLS de confianza
  local ([`mkcert`](https://github.com/FiloSottile/mkcert)) y apunta el
  cliente a la URL `https://` — o aplaza la integración con Claude Desktop a
  la fase de OAuth/TLS y valida con el MCP Inspector por ahora.

El objetivo de esta fase es que **el servidor responda correctamente por
HTTP**; la aceptación completa de Claude Desktop puede tener que esperar a
la fase de auth dependiendo de tu build.

## Diferencias operativas vs stdio

| | stdio | Streamable HTTP |
|---|-------|-----------------|
| Modelo de proceso | Lanzado como subproceso por el cliente. | Servidor de larga duración que arrancas y al que te conectas. |
| Ciclo de vida | Un proceso por sesión de cliente. | Un proceso, muchas peticiones. |
| Lifespan del store | Abierto una vez, reutilizado en la sesión. | Abierto una vez al arrancar, compartido por todas las peticiones. |
| Exposición de red | Ninguna (pipes). | Enlaza un puerto TCP; Origin/Host validados. |
| Sonda de salud | N/A. | `GET /health`. |
| Auth | N/A (confianza local). | Todavía ninguna — solo loopback + comprobación de Origin. |
