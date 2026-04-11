// Engrama — schema initialisation script
// Run once after Neo4j starts:
//   docker exec -i engrama-neo4j cypher-shell -u neo4j -p $NEO4J_PASSWORD < scripts/init-schema.cypher

// === CONSTRAINTS ===

CREATE CONSTRAINT project_name IF NOT EXISTS
  FOR (n:Project) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT technology_name IF NOT EXISTS
  FOR (n:Technology) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT decision_title IF NOT EXISTS
  FOR (n:Decision) REQUIRE n.title IS UNIQUE;

CREATE CONSTRAINT problem_title IF NOT EXISTS
  FOR (n:Problem) REQUIRE n.title IS UNIQUE;

CREATE CONSTRAINT course_name IF NOT EXISTS
  FOR (n:Course) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT concept_name IF NOT EXISTS
  FOR (n:Concept) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT client_name IF NOT EXISTS
  FOR (n:Client) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT insight_title IF NOT EXISTS
  FOR (n:Insight) REQUIRE n.title IS UNIQUE;

// === FULLTEXT INDEX ===

CREATE FULLTEXT INDEX memory_search IF NOT EXISTS
FOR (n:Project|Technology|Decision|Problem|Course|Concept|Client|Insight)
ON EACH [n.name, n.title, n.notes, n.rationale,
         n.solution, n.description, n.context, n.body];

// === RANGE INDEXES ===

CREATE INDEX project_status IF NOT EXISTS
  FOR (n:Project) ON (n.status);

CREATE INDEX problem_status IF NOT EXISTS
  FOR (n:Problem) ON (n.status);

// === VERIFY ===

SHOW CONSTRAINTS;
SHOW INDEXES YIELD name, type, state WHERE state = "ONLINE";
