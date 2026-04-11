# Example Profiles

Reference file for the onboard skill.  Read this when proposing a schema
to the user — use these examples as models, not as constraints.  The user's
graph should reflect how *they* think, not how these examples are structured.

## Table of contents

1. Developer / Technical Instructor
2. Nurse / Researcher / Mother
3. Lawyer / Compliance Officer
4. Product Manager
5. Freelance Creative

---

## 1. Developer / Technical Instructor

Tracks software projects, technology decisions, training courses, and the
concepts that connect them.

```yaml
name: developer
description: Profile for developers and technical instructors

nodes:
  - label: Project
    properties: [name, status, repo, stack, description]
    required: [name]
    description: "A software project or product."
  - label: Technology
    properties: [name, version, type, notes]
    required: [name]
    description: "A language, framework, tool, or infrastructure component."
  - label: Decision
    properties: [title, rationale, date, alternatives]
    required: [title]
    description: "An architectural or technical decision with rationale."
  - label: Problem
    properties: [title, solution, status, context]
    required: [title]
    description: "A bug, blocker, or challenge encountered."
  - label: Course
    properties: [name, cohort, date, level, client]
    required: [name]
    description: "A training course or workshop delivered."
  - label: Concept
    properties: [name, domain, notes]
    required: [name]
    description: "A technical or domain concept."
  - label: Client
    properties: [name, sector, contact]
    required: [name]
    description: "An organisation that commissions work or training."

relations:
  - {type: USES,        from: Project,    to: Technology}
  - {type: INFORMED_BY, from: Project,    to: Decision}
  - {type: HAS,         from: Project,    to: Problem}
  - {type: FOR,         from: Project,    to: Client}
  - {type: ORIGIN_OF,   from: Project,    to: Course}
  - {type: APPLIES,     from: Problem,    to: Concept}
  - {type: SOLVED_BY,   from: Problem,    to: Decision}
  - {type: COVERS,      from: Course,     to: Concept}
  - {type: TEACHES,     from: Course,     to: Technology}
  - {type: IMPLEMENTS,  from: Technology,  to: Concept}
```

---

## 2. Nurse / Researcher / Mother

Tracks patients, clinical protocols, research studies, medications, family
tasks, and the relationships between professional and personal life.

```yaml
name: nurse_researcher
description: Profile for a nurse who is also a scientific researcher and parent

nodes:
  - label: Patient
    properties: [name, condition, ward, status, notes]
    required: [name]
    description: "A patient under care."
  - label: Protocol
    properties: [title, version, specialty, evidence_level]
    required: [title]
    description: "A clinical protocol or procedure guideline."
  - label: Study
    properties: [title, journal, doi, status, findings]
    required: [title]
    description: "A scientific study or research paper."
  - label: Medication
    properties: [name, dosage, route, frequency, notes]
    required: [name]
    description: "A medication or pharmaceutical compound."
  - label: Child
    properties: [name, age, school, notes]
    required: [name]
    description: "A child in the family."
  - label: Task
    properties: [title, due_date, status, context, priority]
    required: [title]
    description: "A personal or professional task or errand."
  - label: Symptom
    properties: [name, severity, notes]
    required: [name]
    description: "A clinical symptom or sign."

relations:
  - {type: RECEIVES,        from: Patient,    to: Medication}
  - {type: FOLLOWS,         from: Patient,    to: Protocol}
  - {type: PRESENTS,        from: Patient,    to: Symptom}
  - {type: CONTRAINDICATES, from: Medication, to: Medication}
  - {type: TREATS,          from: Medication, to: Symptom}
  - {type: SUPPORTS,        from: Study,      to: Protocol}
  - {type: MENTIONS,        from: Study,      to: Medication}
  - {type: ASSIGNED_TO,     from: Task,       to: Child}
```

---

## 3. Lawyer / Compliance Officer

Tracks cases, regulations, contracts, clients, and the precedents that
connect legal arguments across matters.

```yaml
name: lawyer
description: Profile for lawyers and compliance professionals

nodes:
  - label: Case
    properties: [title, status, jurisdiction, court, summary]
    required: [title]
    description: "A legal case or matter."
  - label: Regulation
    properties: [name, code, jurisdiction, effective_date, notes]
    required: [name]
    description: "A law, regulation, or compliance standard."
  - label: Contract
    properties: [title, parties, status, effective_date, value]
    required: [title]
    description: "A contract or legal agreement."
  - label: Client
    properties: [name, sector, contact, risk_level]
    required: [name]
    description: "An individual or organisation represented."
  - label: Precedent
    properties: [title, citation, court, year, ruling]
    required: [title]
    description: "A legal precedent or landmark ruling."
  - label: Concept
    properties: [name, domain, notes]
    required: [name]
    description: "A legal concept or doctrine."

relations:
  - {type: INVOLVES,     from: Case,       to: Client}
  - {type: GOVERNED_BY,  from: Case,       to: Regulation}
  - {type: CITES,        from: Case,       to: Precedent}
  - {type: APPLIES,      from: Case,       to: Concept}
  - {type: BOUND_BY,     from: Contract,   to: Regulation}
  - {type: BETWEEN,      from: Contract,   to: Client}
  - {type: ESTABLISHES,  from: Precedent,  to: Concept}
```

---

## 4. Product Manager

Tracks products, features, user feedback, OKRs, competitors, and the
strategic connections between them.

```yaml
name: product_manager
description: Profile for product managers and strategists

nodes:
  - label: Product
    properties: [name, status, stage, market, description]
    required: [name]
    description: "A product or service being managed."
  - label: Feature
    properties: [title, status, priority, effort, impact]
    required: [title]
    description: "A product feature or capability."
  - label: Feedback
    properties: [title, source, sentiment, status, verbatim]
    required: [title]
    description: "User feedback, interview insight, or support ticket theme."
  - label: Objective
    properties: [title, quarter, status, metric, target]
    required: [title]
    description: "An OKR, goal, or strategic objective."
  - label: Competitor
    properties: [name, market, strengths, weaknesses, notes]
    required: [name]
    description: "A competing product or company."
  - label: Segment
    properties: [name, size, characteristics, notes]
    required: [name]
    description: "A customer segment or persona."

relations:
  - {type: HAS,           from: Product,    to: Feature}
  - {type: REQUESTED_BY,  from: Feature,    to: Segment}
  - {type: DRIVES,        from: Feedback,   to: Feature}
  - {type: SUPPORTS,      from: Feature,    to: Objective}
  - {type: TARGETS,       from: Product,    to: Segment}
  - {type: COMPETES_WITH, from: Product,    to: Competitor}
  - {type: OFFERS,        from: Competitor, to: Feature}
```

---

## 5. Freelance Creative

Tracks projects, clients, skills, invoices, and the portfolio connections
across different types of creative work.

```yaml
name: freelance_creative
description: Profile for freelance designers, writers, or artists

nodes:
  - label: Project
    properties: [name, status, type, deadline, budget, description]
    required: [name]
    description: "A creative project or commission."
  - label: Client
    properties: [name, industry, contact, payment_terms, notes]
    required: [name]
    description: "A client or agency."
  - label: Skill
    properties: [name, level, category, notes]
    required: [name]
    description: "A creative or technical skill."
  - label: Invoice
    properties: [title, amount, status, due_date, notes]
    required: [title]
    description: "An invoice or payment record."
  - label: Asset
    properties: [name, type, format, location, notes]
    required: [name]
    description: "A reusable asset — template, font, photo, component."
  - label: Idea
    properties: [title, status, medium, notes]
    required: [title]
    description: "A creative idea or concept for future work."

relations:
  - {type: FOR,         from: Project,  to: Client}
  - {type: REQUIRES,    from: Project,  to: Skill}
  - {type: USES,        from: Project,  to: Asset}
  - {type: BILLED_BY,   from: Project,  to: Invoice}
  - {type: PAYS,        from: Client,   to: Invoice}
  - {type: INSPIRES,    from: Idea,     to: Project}
```

---

## Design principles across all profiles

When proposing a schema to a new user, keep these principles in mind:

1. **5–8 node types** is the sweet spot.  More than 10 becomes hard to
   remember; fewer than 4 doesn't capture enough structure.
2. **Use `title` for sentence-like identifiers** (decisions, problems, tasks,
   protocols, studies, cases, features, feedback).  Use `name` for proper
   nouns and short labels (projects, people, technologies, medications).
3. **Always include `status`** on nodes with a lifecycle — it enables the
   reflect skill to distinguish open from resolved problems.
4. **Relationships should be verbs** — USES, FOLLOWS, TREATS, CITES.
   Not nouns (USAGE) or adjectives (RELATED).
5. **Direction matters** — `Patient RECEIVES Medication` reads naturally.
   The arrow goes from subject to object.
6. **Don't over-model** — start lean, iterate.  The user can always re-run
   the onboard skill to add node types later.
