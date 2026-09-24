// Engrama — Neo4j runtime schema (packaged with the wheel)
//
// This is the *self-bootstrapping* counterpart to scripts/init-schema.cypher.
// It is applied automatically by Neo4jAsyncStore.ensure_schema() on connect,
// so a fresh Neo4j (e.g. a headless/SaaS pod where the repo's scripts/ dir is
// not present) gets the fulltext index and constraints without anyone running
// `engrama init` by hand.
//
// Two deliberate differences from scripts/init-schema.cypher:
//   * Every statement is idempotent — `IF NOT EXISTS` / `IF EXISTS`. This runs
//     on EVERY connect, possibly against a populated graph, so it must never
//     drop and rebuild an index that is in use. The only DROPs retire the
//     legacy name-only constraints (see CONSTRAINTS below) and are no-ops
//     once those are gone.
//   * No SHOW statements (they are interactive verification, not schema DDL).
//
// Keep the label/property coverage in sync with scripts/init-schema.cypher and
// engrama/core/schema.py when the profile changes.

// === CONSTRAINTS ===
// A node's identity is (label, key, owner): the key is unique per
// (org_id, user_id), so two owners writing the same name get two nodes.
// Older schemas made the key unique on its own. Each label's legacy
// constraint is dropped here (a no-op once gone) and replaced by the
// owner-scoped one plus a plain key index for by-name reads. If the DROP
// cannot run, the legacy constraint stays in force and a same-named write
// by a second owner is rejected with a constraint error.

DROP CONSTRAINT project_name IF EXISTS;
CREATE CONSTRAINT project_name_owner IF NOT EXISTS FOR (n:Project) REQUIRE (n.name, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX project_name_key IF NOT EXISTS FOR (n:Project) ON (n.name);
DROP CONSTRAINT concept_name IF EXISTS;
CREATE CONSTRAINT concept_name_owner IF NOT EXISTS FOR (n:Concept) REQUIRE (n.name, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX concept_name_key IF NOT EXISTS FOR (n:Concept) ON (n.name);
DROP CONSTRAINT decision_title IF EXISTS;
CREATE CONSTRAINT decision_title_owner IF NOT EXISTS FOR (n:Decision) REQUIRE (n.title, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX decision_title_key IF NOT EXISTS FOR (n:Decision) ON (n.title);
DROP CONSTRAINT problem_title IF EXISTS;
CREATE CONSTRAINT problem_title_owner IF NOT EXISTS FOR (n:Problem) REQUIRE (n.title, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX problem_title_key IF NOT EXISTS FOR (n:Problem) ON (n.title);
DROP CONSTRAINT technology_name IF EXISTS;
CREATE CONSTRAINT technology_name_owner IF NOT EXISTS FOR (n:Technology) REQUIRE (n.name, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX technology_name_key IF NOT EXISTS FOR (n:Technology) ON (n.name);
DROP CONSTRAINT person_name IF EXISTS;
CREATE CONSTRAINT person_name_owner IF NOT EXISTS FOR (n:Person) REQUIRE (n.name, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX person_name_key IF NOT EXISTS FOR (n:Person) ON (n.name);
DROP CONSTRAINT domain_name IF EXISTS;
CREATE CONSTRAINT domain_name_owner IF NOT EXISTS FOR (n:Domain) REQUIRE (n.name, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX domain_name_key IF NOT EXISTS FOR (n:Domain) ON (n.name);
DROP CONSTRAINT client_name IF EXISTS;
CREATE CONSTRAINT client_name_owner IF NOT EXISTS FOR (n:Client) REQUIRE (n.name, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX client_name_key IF NOT EXISTS FOR (n:Client) ON (n.name);
DROP CONSTRAINT target_name IF EXISTS;
CREATE CONSTRAINT target_name_owner IF NOT EXISTS FOR (n:Target) REQUIRE (n.name, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX target_name_key IF NOT EXISTS FOR (n:Target) ON (n.name);
DROP CONSTRAINT vulnerability_title IF EXISTS;
CREATE CONSTRAINT vulnerability_title_owner IF NOT EXISTS FOR (n:Vulnerability) REQUIRE (n.title, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX vulnerability_title_key IF NOT EXISTS FOR (n:Vulnerability) ON (n.title);
DROP CONSTRAINT technique_name IF EXISTS;
CREATE CONSTRAINT technique_name_owner IF NOT EXISTS FOR (n:Technique) REQUIRE (n.name, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX technique_name_key IF NOT EXISTS FOR (n:Technique) ON (n.name);
DROP CONSTRAINT tool_name IF EXISTS;
CREATE CONSTRAINT tool_name_owner IF NOT EXISTS FOR (n:Tool) REQUIRE (n.name, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX tool_name_key IF NOT EXISTS FOR (n:Tool) ON (n.name);
DROP CONSTRAINT ctf_name IF EXISTS;
CREATE CONSTRAINT ctf_name_owner IF NOT EXISTS FOR (n:CTF) REQUIRE (n.name, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX ctf_name_key IF NOT EXISTS FOR (n:CTF) ON (n.name);
DROP CONSTRAINT course_name IF EXISTS;
CREATE CONSTRAINT course_name_owner IF NOT EXISTS FOR (n:Course) REQUIRE (n.name, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX course_name_key IF NOT EXISTS FOR (n:Course) ON (n.name);
DROP CONSTRAINT exercise_title IF EXISTS;
CREATE CONSTRAINT exercise_title_owner IF NOT EXISTS FOR (n:Exercise) REQUIRE (n.title, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX exercise_title_key IF NOT EXISTS FOR (n:Exercise) ON (n.title);
DROP CONSTRAINT material_name IF EXISTS;
CREATE CONSTRAINT material_name_owner IF NOT EXISTS FOR (n:Material) REQUIRE (n.name, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX material_name_key IF NOT EXISTS FOR (n:Material) ON (n.name);
DROP CONSTRAINT photo_title IF EXISTS;
CREATE CONSTRAINT photo_title_owner IF NOT EXISTS FOR (n:Photo) REQUIRE (n.title, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX photo_title_key IF NOT EXISTS FOR (n:Photo) ON (n.title);
DROP CONSTRAINT location_name IF EXISTS;
CREATE CONSTRAINT location_name_owner IF NOT EXISTS FOR (n:Location) REQUIRE (n.name, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX location_name_key IF NOT EXISTS FOR (n:Location) ON (n.name);
DROP CONSTRAINT species_name IF EXISTS;
CREATE CONSTRAINT species_name_owner IF NOT EXISTS FOR (n:Species) REQUIRE (n.name, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX species_name_key IF NOT EXISTS FOR (n:Species) ON (n.name);
DROP CONSTRAINT gear_name IF EXISTS;
CREATE CONSTRAINT gear_name_owner IF NOT EXISTS FOR (n:Gear) REQUIRE (n.name, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX gear_name_key IF NOT EXISTS FOR (n:Gear) ON (n.name);
DROP CONSTRAINT model_name IF EXISTS;
CREATE CONSTRAINT model_name_owner IF NOT EXISTS FOR (n:Model) REQUIRE (n.name, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX model_name_key IF NOT EXISTS FOR (n:Model) ON (n.name);
DROP CONSTRAINT dataset_name IF EXISTS;
CREATE CONSTRAINT dataset_name_owner IF NOT EXISTS FOR (n:Dataset) REQUIRE (n.name, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX dataset_name_key IF NOT EXISTS FOR (n:Dataset) ON (n.name);
DROP CONSTRAINT experiment_title IF EXISTS;
CREATE CONSTRAINT experiment_title_owner IF NOT EXISTS FOR (n:Experiment) REQUIRE (n.title, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX experiment_title_key IF NOT EXISTS FOR (n:Experiment) ON (n.title);
DROP CONSTRAINT pipeline_name IF EXISTS;
CREATE CONSTRAINT pipeline_name_owner IF NOT EXISTS FOR (n:Pipeline) REQUIRE (n.name, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX pipeline_name_key IF NOT EXISTS FOR (n:Pipeline) ON (n.name);
DROP CONSTRAINT insight_title IF EXISTS;
CREATE CONSTRAINT insight_title_owner IF NOT EXISTS FOR (n:Insight) REQUIRE (n.title, n.org_id, n.user_id) IS UNIQUE;
CREATE INDEX insight_title_key IF NOT EXISTS FOR (n:Insight) ON (n.title);

// === FULLTEXT INDEX ===
// `n.origin` is the caller-supplied semantic provenance (distinct from the
// system-managed `n.source` transport bucket); both are indexed so a search
// can filter by either.

CREATE FULLTEXT INDEX memory_search IF NOT EXISTS
FOR (n:Project|Concept|Decision|Problem|Technology|Person|Domain|Client|Target|Vulnerability|Technique|Tool|CTF|Course|Exercise|Material|Photo|Location|Species|Gear|Model|Dataset|Experiment|Pipeline|Insight)
ON EACH [n.name, n.status, n.repo, n.description, n.domain, n.notes, n.title, n.rationale, n.alternatives, n.solution, n.context, n.severity, n.version, n.type, n.role, n.organisation, n.contact, n.sector, n.ip, n.os, n.scope, n.cve, n.mitre_id, n.tactic, n.platform, n.difficulty, n.writeup_path, n.cohort, n.level, n.duration, n.format, n.location, n.species, n.camera, n.lens, n.region, n.coordinates, n.habitat, n.family, n.conservation_status, n.brand, n.provider, n.source, n.origin, n.size, n.metric, n.result, n.steps, n.body, n.summary, n.details, n.tags];

// === VECTOR INDEX (DDR-003) ===

CREATE VECTOR INDEX memory_vectors IF NOT EXISTS
FOR (n:Embedded) ON (n.embedding)
OPTIONS {indexConfig: {
  `vector.dimensions`: 768,
  `vector.similarity_function`: 'cosine'
}};

// === RANGE INDEXES ===

CREATE INDEX project_status IF NOT EXISTS FOR (n:Project) ON (n.status);
CREATE INDEX decision_status IF NOT EXISTS FOR (n:Decision) ON (n.status);
CREATE INDEX problem_status IF NOT EXISTS FOR (n:Problem) ON (n.status);
CREATE INDEX target_status IF NOT EXISTS FOR (n:Target) ON (n.status);
CREATE INDEX vulnerability_status IF NOT EXISTS FOR (n:Vulnerability) ON (n.status);
CREATE INDEX ctf_status IF NOT EXISTS FOR (n:CTF) ON (n.status);
CREATE INDEX course_status IF NOT EXISTS FOR (n:Course) ON (n.status);
CREATE INDEX exercise_status IF NOT EXISTS FOR (n:Exercise) ON (n.status);
CREATE INDEX material_status IF NOT EXISTS FOR (n:Material) ON (n.status);
CREATE INDEX photo_status IF NOT EXISTS FOR (n:Photo) ON (n.status);
CREATE INDEX experiment_status IF NOT EXISTS FOR (n:Experiment) ON (n.status);
CREATE INDEX pipeline_status IF NOT EXISTS FOR (n:Pipeline) ON (n.status);
CREATE INDEX insight_status IF NOT EXISTS FOR (n:Insight) ON (n.status);

// === SPEC 001 — composite (org_id, user_id) indexes per queried label ===
// One range index per label so Neo4j can use the index on every scoped MATCH
// (fulltext_search, count_labels, lookup_node_label, get_neighbours, all
// reflect detect_* patterns, all Insight queries). Composite indexes are
// inherently per-label in Neo4j, so each label that the application MATCHes
// on gets its own. New labels added to NodeType MUST also gain a composite
// index here; the T014 CI guard will surface gaps in coverage.

CREATE INDEX project_scope IF NOT EXISTS FOR (n:Project) ON (n.org_id, n.user_id);
CREATE INDEX concept_scope IF NOT EXISTS FOR (n:Concept) ON (n.org_id, n.user_id);
CREATE INDEX decision_scope IF NOT EXISTS FOR (n:Decision) ON (n.org_id, n.user_id);
CREATE INDEX problem_scope IF NOT EXISTS FOR (n:Problem) ON (n.org_id, n.user_id);
CREATE INDEX technology_scope IF NOT EXISTS FOR (n:Technology) ON (n.org_id, n.user_id);
CREATE INDEX person_scope IF NOT EXISTS FOR (n:Person) ON (n.org_id, n.user_id);
CREATE INDEX domain_scope IF NOT EXISTS FOR (n:Domain) ON (n.org_id, n.user_id);
CREATE INDEX client_scope IF NOT EXISTS FOR (n:Client) ON (n.org_id, n.user_id);
CREATE INDEX target_scope IF NOT EXISTS FOR (n:Target) ON (n.org_id, n.user_id);
CREATE INDEX vulnerability_scope IF NOT EXISTS FOR (n:Vulnerability) ON (n.org_id, n.user_id);
CREATE INDEX technique_scope IF NOT EXISTS FOR (n:Technique) ON (n.org_id, n.user_id);
CREATE INDEX tool_scope IF NOT EXISTS FOR (n:Tool) ON (n.org_id, n.user_id);
CREATE INDEX ctf_scope IF NOT EXISTS FOR (n:CTF) ON (n.org_id, n.user_id);
CREATE INDEX course_scope IF NOT EXISTS FOR (n:Course) ON (n.org_id, n.user_id);
CREATE INDEX exercise_scope IF NOT EXISTS FOR (n:Exercise) ON (n.org_id, n.user_id);
CREATE INDEX material_scope IF NOT EXISTS FOR (n:Material) ON (n.org_id, n.user_id);
CREATE INDEX photo_scope IF NOT EXISTS FOR (n:Photo) ON (n.org_id, n.user_id);
CREATE INDEX location_scope IF NOT EXISTS FOR (n:Location) ON (n.org_id, n.user_id);
CREATE INDEX species_scope IF NOT EXISTS FOR (n:Species) ON (n.org_id, n.user_id);
CREATE INDEX gear_scope IF NOT EXISTS FOR (n:Gear) ON (n.org_id, n.user_id);
CREATE INDEX model_scope IF NOT EXISTS FOR (n:Model) ON (n.org_id, n.user_id);
CREATE INDEX dataset_scope IF NOT EXISTS FOR (n:Dataset) ON (n.org_id, n.user_id);
CREATE INDEX experiment_scope IF NOT EXISTS FOR (n:Experiment) ON (n.org_id, n.user_id);
CREATE INDEX pipeline_scope IF NOT EXISTS FOR (n:Pipeline) ON (n.org_id, n.user_id);
CREATE INDEX insight_scope IF NOT EXISTS FOR (n:Insight) ON (n.org_id, n.user_id);
