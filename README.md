# jobbot

A bot that collects the job openings in a country from public job APIs,
stores them in a local SQLite database, keeps them up to date, and exports
them to CSV or JSON. Run it once, or leave it running to get new openings
posted to Slack or Discord.

```console
$ jobbot fetch --country sweden
Sweden (SE) — finished in 13m 16s

  source           mode         status       found       new   closed  requests
  jobtech          full         ok          44,596    44,596        0         1

Open jobs stored for Sweden: 44,596 (44,596 unique across sources)

$ jobbot fetch --country sweden        # later: only what changed
Sweden (SE) — finished in 0m 01s

  source           mode         status       found       new   closed  requests
  jobtech          incremental  ok              41         2        1         1

Open jobs stored for Sweden: 44,597 (44,597 unique across sources)

New since the last run: 2
  • Butiksmedarbetare Gottis — Amoude AB (Uppsala, Uppsala län) https://arbetsformedlingen.se/platsbanken/annonser/31784275
  • Dynamics 365 CE Functional Analyst — Avanade Sweden AB (Stockholm, Stockholms län) https://arbetsformedlingen.se/platsbanken/annonser/31784227
```

## Which countries, and how complete?

No single API lists every job in a country, so jobbot combines several kinds
of source. It removes listings that appear on more than one source, and it
tells you when a crawl could not reach everything.

| Source | Countries | What it covers | Key |
| --- | --- | --- | --- |
| `jobtech` | Sweden | Every ad on Platsbanken, the public employment service's board (~45k) | none |
| `arbeitsagentur` | Germany | The Federal Employment Agency's board (~800k jobs) | none |
| `adzuna` | AT AU BE BR CA CH DE ES FR GB IN IT MX NL NZ PL SG US ZA | A search engine that aggregates most job boards in each country | free |
| `usajobs` | US | US federal government jobs | free |
| `greenhouse`, `lever`, `ashby`, `smartrecruiters` | any | Career sites of the companies you list | none |

* **Sweden and Germany** are close to complete: they are the national public
  job boards, and no key is needed.
* **Adzuna** is the broadest source for the other 17 countries. Free keys have
  a daily request limit (around 250 requests, or 12,500 listings, when this was
  written), so a large country fills in over several runs, newest first. Raise
  `max_requests` in `jobbot.toml` if Adzuna gives you a higher limit.
* **Company boards** add employers' own postings in any country. You list the
  companies in `jobbot.toml`.

Sites such as LinkedIn, Indeed and SEEK have no public job-search API and
forbid scraping, so jobbot does not use them.

Run `jobbot sources --country <code>` to see what is available for a country
and what each source still needs.

## Setup

Python 3.11 or newer.

```bash
pip install -e .            # installs the `jobbot` command (only dependency: requests)
jobbot sources --country nz
```

API keys, for the sources that need one, come from environment variables:

```bash
export ADZUNA_APP_ID=...    ADZUNA_APP_KEY=...     # https://developer.adzuna.com/
export USAJOBS_API_KEY=...  USAJOBS_EMAIL=you@example.com   # https://developer.usajobs.gov/
```

Optional settings (company boards, request budgets, extra place names) go in
`jobbot.toml`. Copy [`jobbot.example.toml`](jobbot.example.toml) to start.

## Usage

```bash
jobbot fetch --country de                     # crawl, store in ./jobbot.db, print a summary
jobbot fetch --country nz --out nz.csv        # ...and export the open jobs
jobbot fetch --country se --since-days 1      # only jobs posted or changed in the last day
jobbot fetch --country us --source usajobs    # just one source
jobbot export --country de --out de.jsonl     # export without crawling (csv, json or jsonl)
jobbot stats                                  # what is stored, and recent crawls
```

`--country` takes a code (`nz`, `GB`) or a name (`"New Zealand"`, `UK`).

### Running it as a bot

`watch` repeats the crawl on a schedule and posts new openings to a webhook.
It understands Slack and Discord webhook URLs and sends plain JSON to anything else:

```bash
jobbot watch --country nz --every 6h --webhook https://hooks.slack.com/services/...
```

Or schedule single runs with cron:

```cron
0 */6 * * *  cd /path/to/jobbot && jobbot fetch --country nz -q --webhook "$JOBBOT_WEBHOOK_URL"
```

The first crawl of a source loads everything it has, and that load is not
posted to the webhook. After that, each run posts the openings that are new
since the previous run.

## How it works

* **First run:** a full crawl of every ready source.
* **Later runs:** only what changed since the last successful crawl, where
  the source supports that. Germany, Adzuna and USAJOBS do a full crawl again
  every 7 days, because their change feeds don't report removed jobs. Company
  boards are always read in full.
* **Closing jobs:** a job is marked closed when its source reports it removed,
  or when a complete full crawl no longer lists it. Crawls that stop early
  (request budget, errors) never close anything. A closed job that reappears
  is reopened.
* **Duplicates:** the same title, company and place on two sources counts
  once in exports and summaries; the `sources` column lists where it appeared.
  Use `--no-dedupe` to keep every copy.
* **Germany:** one query to the federal board can only page through 10,000
  results. jobbot splits the search into slices (by occupational field, temp
  agency, contract type and private agency, then overlapping 150 km circles
  around cities) until each slice fits, and reports how many listings it
  reached. Jobs the board lists abroad are left out.
* **Sweden:** the first crawl downloads the full snapshot of published ads,
  about half a gigabyte. Later crawls read the change feed, which also reports
  removed ads.

### Output columns

`country, title, company, location, city, region, work_mode, employment_type,
category, salary_min, salary_max, salary_currency, salary_period, salary_text,
posted_at, expires_at, url, source, source_id, sources, first_seen, last_seen,
closed_at, description`

Fields a source does not provide are left empty. The German board and
SmartRecruiters give no description in their listings, and Swedish and German
listings use the language of the original ad.

### Known limits

* Greenhouse boards give free-text locations, such as "Remote, US" or
  "London, ON". jobbot reads them with a list of country names, states,
  provinces and major cities. A posting that names only a small town can be
  missed: add such places under `[locations]` in `jobbot.toml`.
* A remote job open to a whole region ("Remote – EMEA") is not counted for
  each country in that region.
* The Adzuna and USAJOBS sources follow those APIs' published formats but have
  not yet been run against the live services. The keyless sources have.

## Development

```bash
pip install -e '.[test]'
pytest
```

To add a source, subclass `jobbot.sources.base.Source`, implement
`fetch(country, ctx)` so it yields `Job` objects (and `Removed` for postings a
change feed reports deleted), then add it to `ALL_SOURCES` in
`jobbot/sources/__init__.py`.
