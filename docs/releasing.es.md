# Publicar engrama

El pipeline de release se encuentra en [`.github/workflows/release.yml`](https://github.com/scops/engrama/blob/main/.github/workflows/release.yml).
Se activa con un `push` de cualquier tag que coincida con `v*`, ejecuta un pipeline de seis etapas
(`guardian → build → sbom → attest → publish → release-notes`) y produce:

- Un release en PyPI publicado vía **trusted publishing** (OIDC, sin API key).
- Un GitHub Release con la wheel, el sdist y los SBOMs adjuntos.
- Atestaciones de procedencia de build SLSA tanto en la wheel como en el sdist (verificables desde la UI de GitHub y vía `gh attestation verify`).
- Atestaciones PEP 740 en PyPI (generadas por `pypa/gh-action-pypi-publish`).

## Configuración inicial (trusted publishing en PyPI)

Necesaria antes del **primer** release que llegue a PyPI. El workflow ya
está preparado para OIDC; PyPI solo necesita saber en qué workflow
confiar.

1. Iniciad sesión en <https://pypi.org/> con una cuenta propietaria (o futura propietaria) del proyecto `engrama`.
2. Id a **Manage project → Publishing** (o **Your projects → Add a new publisher** si el proyecto aún no existe).
3. Haced clic en **Add a new pending publisher** (o **Add a new publisher** si el proyecto ya existe) y rellenad:
   - **PyPI Project Name:** `engrama`
   - **Owner:** `scops`
   - **Repository name:** `engrama`
   - **Workflow name:** `release.yml`
   - **Environment name:** `pypi`
4. Guardad. Los pushes de tags posteriores se publicarán automáticamente.

El mismo procedimiento en <https://test.pypi.org/> si queréis ensayar primero contra TestPyPI — registrad un pending publisher allí con el nombre de entorno `testpypi` y apuntad el workflow hacia él (la versión publicada de este workflow no apunta actualmente a TestPyPI; añadid un job paralelo `publish-test` si lo necesitáis).

## Crear un release

El release se dirige enteramente por el commit de subida de versión y el tag.
No hay ningún paso manual dentro del workflow.

1. Elegid una versión SemVer (p. ej. `0.9.1`).
2. Actualizadla en tres sitios — `guardian` rechazará publicar si alguno diverge:
   - `pyproject.toml` — `version = "..."`
   - `engrama/__init__.py` — `__version__ = "..."`
   - `changelog.md` — añadid un nuevo encabezado `## [X.Y.Z] — YYYY-MM-DD` en la parte superior, con las notas del release para esta versión. Mantened el formato del encabezado intacto; el job `release-notes` lo parsea.
3. Haced commit y abrid un PR titulado `release: vX.Y.Z`. Mergeadlo a `main` tras pasar CI.
4. Etiquetad el merge commit y haced push:

   ```bash
   git checkout main && git pull
   git tag -a vX.Y.Z -m "engrama vX.Y.Z"
   git push origin vX.Y.Z
   ```

5. Observad la ejecución en <https://github.com/scops/engrama/actions/workflows/release.yml>. Si `guardian` falla, corregid la divergencia en `main`, borrad el tag local y remotamente, y volved a etiquetar.

## Ejecución en seco de un release

`workflow_dispatch` tiene un toggle `dry_run` (por defecto `true`).
Usadlo para validar `guardian + build + sbom + attest` sin publicar
ni crear un Release:

1. Actions → **Release** → **Run workflow**.
2. Introducid el tag candidato (p. ej. `v0.9.1`) y dejad `dry_run: true`.
3. El pipeline ejecuta todo hasta SBOM + attest. `publish` y `release-notes` se saltan mediante guardas `if:`.

## Prueba local de la wheel

Antes de etiquetar, pasad la wheel por un venv limpio para detectar
errores de empaquetado que el job `import-smoke` del CI matricial no
puede ver — p. ej. un archivo que falta en `MANIFEST.in`, un extra
`[mcp]` roto o un entry point del CLI que casca en una instalación base:

```bash
# 1. Build
uv build --sdist --wheel

# 2. Venv limpio con el python del sistema (NO uv venv — evitad la caché de resolución de uv)
python -m venv /tmp/engrama-cleantest
source /tmp/engrama-cleantest/bin/activate   # PowerShell: . /tmp/engrama-cleantest/Scripts/Activate.ps1

# 3. Solo instalación base
pip install dist/engrama-*.whl

# 4. Salid del directorio del repo (para que el árbol fuente no haga sombra al paquete instalado)
cd /tmp

# 5. Comprobad que la ruta de importación apunta al venv, no al árbol fuente
python -c "import engrama; print(engrama.__file__)"
# Esperado: .../engrama-cleantest/.../site-packages/engrama/__init__.py

# 6. Ejercitad la ruta SQLite sin configuración de extremo a extremo
python -c "from engrama import Engrama; \
  e = Engrama(); e.remember('Concept', 'Smoke', 'works'); \
  print('hits:', len(e.recall('Smoke', hops=0)))"

# 7. Entry points del CLI
engrama --help
engrama-mcp --help   # debe fallar con un mensaje de instalación CLARO, no con un traceback

# 8. Ahora instalad el extra [mcp] y comprobad de nuevo
pip install "dist/engrama-*.whl[mcp]"
engrama-mcp --help   # debería mostrar el bloque de uso
```

Si en el paso 7 la llamada a `engrama-mcp` sin el extra muestra un traceback
de Python en lugar de un mensaje de instalación de una línea, el handler de
error amigable en `engrama/adapters/mcp/__init__.py` ha regresionado —
corregidlo antes de etiquetar.

## Verificar un release

Después de que un release aterrice, podéis verificar que los artefactos fueron construidos por este workflow exacto:

```bash
gh attestation verify --owner scops <path-to-wheel-or-sdist>
```

Los SBOMs (`engrama-X.Y.Z.cyclonedx.json`, `engrama-X.Y.Z.spdx.json`)
adjuntos al GitHub Release son los SBOMs canónicos para enviar a
herramientas SCA o a adquisiciones empresariales — reflejan lo que
realmente se construyó y publicó, no solo lo que el grafo de
dependencias cree.

## Qué hay dónde

| Aspecto                  | Ubicación                                 |
| ------------------------ | ----------------------------------------- |
| Workflow                 | `.github/workflows/release.yml`           |
| Puerta de deriva de versión | job `guardian`                          |
| Build (wheel + sdist)    | job `build` — `uv build`                  |
| SBOM CycloneDX + SPDX    | job `sbom` — `cyclonedx-bom` + `syft`     |
| SBOM pip-audit (cruce)   | job `sbom`                                |
| Procedencia de build SLSA | job `attest` — `actions/attest-build-provenance` |
| Publicación en PyPI (OIDC) | job `publish` — `pypa/gh-action-pypi-publish` con `attestations: true` |
| GitHub Release           | job `release-notes` — extrae la entrada superior del CHANGELOG |
| Puerta de vulnerabilidades en PR | job `audit-deps` en `.github/workflows/ci.yml` |
