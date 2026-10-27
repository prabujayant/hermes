# House Style - owlsky17 Threads

Locked 2026-10-27 from the 50 all-time most-viewed posts.
Evidence: 50 posts, 25,727 views, top post 6,133, median hook 97 chars (range 45-296).
This file is the source of truth for every hourly thread. Topic and angle may change. Format may not.

## The winning mix

Measured from the top 50:

- 35/50 ALL-CAPS listicles: 23 `IF YOU WANT...`, 12 `THINGS...`.
- 13/50 sentence-case technical posts: 10 statements, 3 first-person stories.
- 1 question, 1 other.
- Topic hits overlap: career/senior 25, system design/interviews 21, SQL/Postgres/ORM 13, LeetCode 2, AI coding 2.

Default rotation for every 10 threads:

- 7 caps listicles: 4-5 `IF YOU WANT...`, 2-3 `THINGS...`.
- 3 sentence-case technical posts: 2 statements, 1 first-person story.
- Topics: 4 career/senior, 3 system design/interviews, 2 SQL/Postgres/ORM, 1 flex.
- The flex slot is backend or LeetCode. AI coding at most 1 in 20 threads, and only when the lesson is really about backend review, SQL, or career judgment.

Never post two threads in a row on the same subject family. Never reuse a hook, opening phrase, or item list from a recent post.

## Thread shape - always 5 posts

1. Hook (post 1)
2. Value (post 2)
3. Value (post 3)
4. Value (post 4)
5. Follow CTA (post 5)

The proven listicle promises 5 things in the hook but delivers 3 value posts plus the CTA. Keep that 5-post shape. If you want the promise to be exact, promise 3 instead. Never add a 6th post to a listicle.

## Hook rules

- 45-300 characters. Target 60-160.
- One idea, one promise, one tension. No setup, no throat-clearing.
- No hashtags. No emojis. No counters like 1/5. No "thread" or "🧵".
- Name the outcome in the reader's language: pass interviews, become senior, fix p99, stop index bloat.
- The hook must fit one of the four formulas below. No invented formats.

### Formula 1 - IF YOU WANT (most common)

```
IF YOU WANT TO <DESIRED OUTCOME>, STOP <COMMON BAD HABIT>.

MEMORIZE THESE <3 OR 5> <NOUNS>:
```

Proven examples:

```
IF YOU WANT TO PASS SYSTEM DESIGN INTERVIEWS, STOP LISTING TECHNOLOGIES.

MEMORIZE THESE 5 MOVES:
```

```
IF YOU WANT TO BECOME A SENIOR ENGINEER FASTER, STOP COUNTING YEARS.

MEMORIZE THESE 5 LEVERS:
```

```
IF YOU WANT TO CRACK CODING INTERVIEWS, STOP PRACTICING RANDOMLY.

MEMORIZE THESE 5 PATTERNS:
```

### Formula 2 - THINGS

```
THINGS <AUDIENCE> CAN'T TELL FROM <ARTIFACT> (SO PROVE THEM IN THE <VENUE>):
```

```
THINGS THAT QUIETLY KILL <DESIRED OUTCOME>:
```

Proven examples:

```
THINGS HIRING MANAGERS CAN'T TELL FROM YOUR RESUME (SO PROVE THEM IN THE INTERVIEW):
```

```
THINGS THAT QUIETLY KILL ENGINEERING CAREERS:
```

Use the AI-coding version rarely, at most 1 in 20 threads.

### Formula 3 - sentence-case technical statement

One blunt claim, the mechanism, then the consequence or number.

```
<Claim>. <Mechanism>. <Consequence or number>.
```

Proven examples:

```
Stop using UUIDs as primary keys in high-write tables. They fragment the buffer pool, bloat indexes 4x, and turn range scans into random I/O. Your p99 latency pays the price.
```

```
Foreign keys are not optional in production. If your ORM makes them painful, change the ORM, not the constraint.
```

```
ORMs are fine until you stop inspecting the SQL they generate.
```

```
If API p99 is rising while Postgres CPU stays low, check pool wait time before adding database capacity.
```

### Formula 4 - first-person technical story

A costly mistake, the mechanism, the measured damage, the fix. Past tense. No name-dropping.

```
I <did a concrete thing>. <Time passes>. <Measurable cost>. <Fix and new number>.
```

Proven example:

```
I used UUIDv4 for every primary key on a payments service because "distributed systems need it." Two years later the buffer pool was 80 percent index pages. The table had 120 million rows. The primary key index alone was 18 GB. A BIGINT version of the same workload sat at 4.2 GB.
```

## Value-post rules

- Posts 2-4. One idea each. No numbering. No bullets. No "First," "Second,".
- Listicle value post: sentence-case heading on its own line, blank line, then 1-2 sentences of proof.
- Technical value post: 1-3 short paragraphs. Mechanism, evidence, caveat. Sentence case.
- Each value post 100-400 characters. Hard max 500.
- Be specific: name the tool, the query, the metric, the failure mode, the number.
- No generic motivation. No "mindset". No "journey". No "game changer".
- No fake employers, clients, credentials, or named people. Technical numbers must be plausible and tied to the mechanism. Never present an invented client win as fact.

Proven listicle value posts:

```
Start with requirements, not Kafka.

Users, scale, latency. Numbers first. The interviewer wants your thinking, not your stack.
```

```
Own outcomes, not tickets.

Juniors close tasks. Seniors move metrics. Ask what number your work changes.
```

```
How you debug when prod is on fire.

Walk through logs, metrics, and your first three commands. Process beats claims.
```

Proven technical value posts:

```
A UUIDv4 is 16 random bytes. InnoDB clusters data by PK. Every insert lands on a random page. The working set explodes. A 10M row table with BIGINT fits in 2GB buffer pool. Same data with UUID needs 8GB just to keep the hot pages resident.
```

```
I learned this on a payments table doing 5K writes/sec. Switched PK from UUID to BIGINT. Index size dropped from 12GB to 3GB. Buffer pool hit rate jumped from 84% to 98%. P99 write latency fell from 45ms to 3ms. No code changes. Just the key type.
```

## Final post - follow CTA, always

Post 5 is never a value post. It is never a question. It is never "comment below".

Default, exactly:

```
I post daily breakdowns like this for backend, SQL, and interviews.

Follow @owlsky17 so you don't miss the next one.
```

For a technical story thread, this shorter version is allowed:

```
Follow @owlsky17 so you don't miss the next one.
```

## Topic bank

### Career / senior (target 4 of 10)

- Years vs outcomes. Ownership vs tickets.
- Visibility: design docs, runbooks, postmortems, mentoring.
- Deleting code, killing flags, simplifying systems.
- Promotion packets, staff scope, on-call judgment.
- Code review behavior, asking questions, being unblocked.
- Resume claims vs interview proof: debugging under fire, real numbers, unblocking others.
- Hiring manager blind spots.

### System design / interviews (target 3 of 10)

- Requirements before technologies. Users, scale, latency.
- Happy path first, then what dies first.
- Tradeoffs and what breaks at 10x.
- Back-of-envelope numbers, data model, API shape.
- Queues, caching, consistency, load balancing, failure modes.
- "Stop listing technologies" angles for interviews.
- Design like a senior: questions before boxes.

### SQL / Postgres / ORM (target 2 of 10)

- UUID vs BIGINT vs UUIDv7 vs ULID: index size, buffer pool, write amplification.
- Index bloat, page splits, range scans, random I/O.
- N+1 queries, ORM-generated SQL, missing indexes.
- Transactions, row locks, external calls inside transactions.
- JSONB as an escape hatch: when it becomes a full-table scan.
- Foreign keys, constraints, cascades, migrations.
- Connection pools, p99, pool wait time, database capacity.
- Query plans, partitioning, replication, Redis locks.

### Flex (target 1 of 10)

- Backend correctness and production failure stories.
- LeetCode patterns only when tied to interview judgment.
- AI coding only as a backend/SQL/career lesson, at most 1 in 20 threads.

## Hard rules

- Plain ASCII only. No em dashes, en dashes, curly quotes, emojis, or hashtags.
- No counters like 1/5 anywhere.
- Every post <= 500 characters.
- Never fake numbers, credentials, or employment history.
- No politics, crypto, general motivation, layoff news, or unrelated trends.
- No AI-coding drift. The top 50 contains only 2 AI-coding posts out of 50.
- Never repeat a recent hook or opening phrase. Vary the subject, verb, and outcome.
- The last post is always the follow CTA.
