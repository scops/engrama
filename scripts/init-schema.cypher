// Engrama — schema initialisation script
// Auto-generated from profile: base+hacking+teaching+photography+ai
// Generated at: 2026-04-12T15:47:30
//
// Run once after Neo4j starts:
//   docker exec -i engrama-neo4j cypher-shell -u neo4j -p $NEO4J_PASSWORD < scripts/init-schema.cypher

// === CONSTRAINTS ===

CREATE CONSTRAINT project_name IF NOT EXISTS
  FOR (n:Project) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT concept_name IF NOT EXISTS
  FOR (n:Concept) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT decision_title IF NOT EXISTS
  FOR (n:Decision) REQUIRE n.title IS UNIQUE;

CREATE CONSTRAINT problem_title IF NOT EXISTS
  FOR (n:Problem) REQUIRE n.title IS UNIQUE;

CREATE CONSTRAINT technology_name IF NOT EXISTS
  FOR (n:Technology) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT person_name IF NOT EXISTS
  FOR (n:Person) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT domain_name IF NOT EXISTS
  FOR (n:Domain) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT client_name IF NOT EXISTS
  FOR (n:Client) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT target_name IF NOT EXISTS
  FOR (n:Target) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT vulnerability_title IF NOT EXISTS
  FOR (n:Vulnerability) REQUIRE n.title IS UNIQUE;

CREATE CONSTRAINT technique_name IF NOT EXISTS
  FOR (n:Technique) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT tool_name IF NOT EXISTS
  FOR (n:Tool) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT ctf_name IF NOT EXISTS
  FOR (n:CTF) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT course_name IF NOT EXISTS
  FOR (n:Course) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT exercise_title IF NOT EXISTS
  FOR (n:Exercise) REQUIRE n.title IS UNIQUE;

CREATE CONSTRAINT material_name IF NOT EXISTS
  FOR (n:Material) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT photo_title IF NOT EXISTS
  FOR (n:Photo) REQUIRE n.title IS UNIQUE;

CREATE CONSTRAINT location_name IF NOT EXISTS
  FOR (n:Location) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT species_name IF NOT EXISTS
  FOR (n:Species) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT gear_name IF NOT EXISTS
  FOR (n:Gear) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT model_name IF NOT EXISTS
  FOR (n:Model) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT dataset_name IF NOT EXISTS
  FOR (n:Dataset) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT experiment_title IF NOT EXISTS
  FOR (n:Experiment) REQUIRE n.title IS UNIQUE;

CREATE CONSTRAINT pipeline_name IF NOT EXISTS
  FOR (n:Pipeline) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT insight_title IF NOT EXISTS
  FOR (n:Insight) REQUIRE n.title IS UNIQUE;

// === FULLTEXT INDEX ===
// DROP + CREATE so that changes to the index definition (e.g. new enrichment
// properties) take effect when `engrama init` re-applies the schema.
// Neo4j fulltext supports string arrays, so `n.tags` is indexable.

DROP INDEX memory_search IF EXISTS;

CREATE FULLTEXT INDEX memory_search
FOR (n:Project|Concept|Decision|Problem|Technology|Person|Domain|Client|Target|Vulnerability|Technique|Tool|CTF|Course|Exercise|Material|Photo|Location|Species|Gear|Model|Dataset|Experiment|Pipeline|Insight)
ON EACH [n.name, n.status, n.repo, n.description, n.domain, n.notes, n.title, n.rationale, n.alternatives, n.solution, n.context, n.severity, n.version, n.type, n.role, n.organisation, n.contact, n.sector, n.ip, n.os, n.scope, n.cve, n.mitre_id, n.tactic, n.platform, n.difficulty, n.writeup_path, n.cohort, n.level, n.duration, n.format, n.location, n.species, n.camera, n.lens, n.region, n.coordinates, n.habitat, n.family, n.conservation_status, n.brand, n.provider, n.source, n.size, n.metric, n.result, n.steps, n.body, n.summary, n.details, n.tags];

// === VECTOR INDEX (DDR-003) ===

CREATE VECTOR INDEX memory_vectors IF NOT EXISTS
FOR (n:Embedded) ON (n.embedding)
OPTIONS {indexConfig: {
  `vector.dimensions`: 768,
  `vector.similarity_function`: 'cosine'
}};

// === RANGE INDEXES ===

CREATE INDEX project_status IF NOT EXISTS
  FOR (n:Project) ON (n.status);

CREATE INDEX decision_status IF NOT EXISTS
  FOR (n:Decision) ON (n.status);

CREATE INDEX problem_status IF NOT EXISTS
  FOR (n:Problem) ON (n.status);

CREATE INDEX target_status IF NOT EXISTS
  FOR (n:Target) ON (n.status);

CREATE INDEX vulnerability_status IF NOT EXISTS
  FOR (n:Vulnerability) ON (n.status);

CREATE INDEX ctf_status IF NOT EXISTS
  FOR (n:CTF) ON (n.status);

CREATE INDEX course_status IF NOT EXISTS
  FOR (n:Course) ON (n.status);

CREATE INDEX exercise_status IF NOT EXISTS
  FOR (n:Exercise) ON (n.status);

CREATE INDEX material_status IF NOT EXISTS
  FOR (n:Material) ON (n.status);

CREATE INDEX photo_status IF NOT EXISTS
  FOR (n:Photo) ON (n.status);

CREATE INDEX experiment_status IF NOT EXISTS
  FOR (n:Experiment) ON (n.status);

CREATE INDEX pipeline_status IF NOT EXISTS
  FOR (n:Pipeline) ON (n.status);

// === VERIFY ===

SHOW CONSTRAINTS;
SHOW INDEXES YIELD name, type, state WHERE state = "ONLINE";
