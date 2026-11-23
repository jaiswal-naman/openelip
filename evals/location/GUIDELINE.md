# Location labelling guideline

Version 1. Rules 1 to 11 and the worked examples were written before any row was labelled. Cases they did not settle
are listed at the end, under "Decisions made while labelling", with the decision taken, so a reviewer can see what was
added after the first draft.

Why this exists: IDX-06 asks that the index state a job's country and city correctly at least 95% of the time on a
labelled sample, and that a place nobody stated stays unknown. This is the written definition of "correct" that the
labels in `sample.jsonl` follow. A wrong country is worse than a null: a parser that returns a place the inputs do not
state has guessed.

## What a row holds

Each row of `sample.jsonl` is the location input of one real public job posting, exactly as its board published it.

| Field | Meaning |
|---|---|
| `location_raw` | Free text the employer typed, or null. |
| `country_hint` | The board's own country field. Lever sends `country`, Ashby sends the postal address country, Greenhouse sends none. A code or a name, or null. |
| `remote_hint` | true, false or null, from the board's workplace-type field. It never changes the two labels. |

You label two things for every row. Null means "these inputs do not say", and a null is a correct label.

- `label_country`: an ISO 3166-1 alpha-2 code in capitals (`US`, `GB`, `DE`, `IN`), or null.
- `label_city`: a city name, or null.

## Rules

1. **The evidence is the row.** Use `location_raw` and `country_hint` only. Never use the company, the board name, the
   language of the posting, a currency, a time zone, or what you know about where a company has offices.
2. **`label_country` is set** when one of these holds, otherwise it is null:
   - a. the text names a country by name, code or common short form ("Germany", "DE", "UK", "U.S.", "Deutschland").
     England, Scotland, Wales and Northern Ireland are `GB`.
   - b. the text names a city, state, province or region that lies in one country, and a reader of English job boards
     would not hesitate ("San Francisco, CA", "Toronto, ON", "Bengaluru", "Bavaria", "Bay Area").
   - c. the country hint names a country and the text does not name a different one. When the text clearly names
     another country, the text wins. When the text is a city that exists in several countries and the hint is one of
     them, the hint settles it ("Cambridge" with hint `US` is `US`).
3. **`label_city` is set** when the text names a city, written in its usual English form: "Munich" for München,
   "Cologne" for Köln, "Kyiv", "Bengaluru" for Bangalore, "Zurich" for Zürich, "New York" for "NYC" and "New York
   City", "Washington" for "Washington, DC". The scorer ignores case and accents and treats alternative spellings as
   equal, so write the common English name, never a transliteration of your own. A district or borough inside a city
   (Manhattan, Shoreditch) is labelled with the city that contains it when you are certain which one. Otherwise null.
4. **A bare city name** gets a country when one city of that name is far larger or better known than every other
   (London, Paris, Berlin, Dublin, Sydney, Toronto, Boston, Austin, Portland). It stays null for the country, with the
   city still labelled, when two or more well-known cities in different countries share the name (Cambridge,
   Birmingham, Manchester, Hamilton, Valencia, Victoria, Richmond) and nothing else in the row settles it.
5. **Two-letter strings.** In "City, XX" the pair is read as a real place: "Austin, TX" is Texas, "Toronto, ON" is
   Ontario, "Berlin, DE" is Germany. A bare "CA", "IN", "DE", "PA", "GA" or similar that could be a country or a US
   state or Canadian province is null unless the hint settles it. A bare country-or-state name ("Georgia") is null
   for the same reason; "Atlanta, Georgia" is `US`.
6. **Several places in one string** (separated by semicolons, slashes, pipes, the words "or" and "and", or newlines):
   label the first one listed. Words that say how the work is done (Remote, Hybrid, On-site, Flexible) are not places
   and are skipped. A region is a place that carries no country, so "EMEA; London" is null and null, while
   "Remote; London" is `GB` and London. Parts of one place ("San Francisco, CA, United States") are one place.
7. **Regions that cross countries are null and null:** EMEA, APAC, LATAM, DACH, Nordics, Europe, Asia, North America,
   the Americas, Global, Worldwide, Anywhere, International, "Multiple locations", "Various", "HQ", "Headquarters",
   "Office", "TBD", "N/A". So are time zones ("Remote - EST", "PST hours"). Apply rule 2c afterwards: with a country
   hint the country is the hint's and the city stays null.
8. **A region inside one country** gives the country and no city: "Bay Area", "Silicon Valley", "California",
   "Bavaria", "Ontario". A metro phrased around one city ("Greater Boston Area", "New York Metro", "Greater London")
   gives that city.
9. **"Remote" alone is null and null. "Remote - US" is `US` and null.** "Remote - Berlin" is `DE` and Berlin.
10. **No text and no hint** is null and null. No text with a hint is the hint's country and a null city.
11. **Typos and other languages** are labelled by what the string plainly means ("San Fransisco", "München, Deutschland").
    A string that is not a place at all (a job code, a team name) is null and null.

## Worked examples

The inputs below are generic location strings, not rows of the sample.

| `location_raw` | `country_hint` | `label_country` | `label_city` | Rule |
|---|---|---|---|---|
| San Francisco, CA | null | US | San Francisco | 2b, 5 |
| Austin, Texas, United States | null | US | Austin | 2a |
| London | null | GB | London | 4 |
| Cambridge | null | null | Cambridge | 4: UK and US both plausible |
| Cambridge | US | US | Cambridge | 2c |
| Cambridge, MA | null | US | Cambridge | 5 |
| Remote | null | null | null | 9 |
| Remote | US | US | null | 9, 2c |
| Remote - US | null | US | null | 9 |
| Remote, EMEA | null | null | null | 7 |
| California | null | US | null | 8 |
| CA | null | null | null | 5: California or Canada |
| Toronto, ON | null | CA | Toronto | 5 |
| Munich (Hybrid) | null | DE | Munich | 3, 4 |
| München, Deutschland | null | DE | Munich | 3, 11 |
| Bangalore, KA | null | IN | Bengaluru | 3 |
| New York, NY; San Francisco, CA | null | US | New York | 6 |
| London / Berlin | null | GB | London | 6 |
| NYC | null | US | New York | 3 |
| Bay Area | null | US | null | 8 |
| Greater Boston Area | null | US | Boston | 8 |
| Singapore | null | SG | Singapore | 2a, 3 |
| Georgia | null | null | null | 5 |
| Atlanta, Georgia | null | US | Atlanta | 5 |
| Multiple Locations | null | null | null | 7 |
| Hybrid | DE | DE | null | 2c |
| null | United Kingdom | GB | null | 10 |
| null | null | null | null | 10 |
| Remote - CA | null | null | null | 5 |
| Paris, TX | null | US | Paris | 5 |
| Remote - Brazil | null | BR | null | 9 |
| Berlin | US | DE | Berlin | 2c: the text names its own country |

## Decisions made while labelling

These came up while labelling the 220 distinct inputs of the sample. Each was decided once and applied to every row
with the same input. Counts are rows of `sample.jsonl`.

1. **Country-code prefixes** ("US-CA-Menlo Park", "GB-London", "IN-Bangalore-MSO", "CH-Zurich-Observe"; 17 rows) are read
   as country, optional state, city, then an office or team code that is ignored. The state code settles an ambiguous
   city: "US-CA-Dublin" (2 rows) is `US` and Dublin, never `IE` (rule 5).
2. **A facility or office word beside a place** ("Dallas Warehouse", "New York, NY (HQ)", "Bengaluru-VTP, India",
   "NYC-Privy"; 16 rows) does not change the place: the place is labelled.
3. **A country followed by a placeholder or qualifier** ("Mexico - Update Location", "Ukraine Anywhere"; 3 rows) is the
   country and a null city.
4. **Abbreviations.** "NYC" and "SF" are read as New York and San Francisco (5 rows). "SEA" alone (1 row) is null and
   null: it is Seattle's airport code and also short for Southeast Asia, so a reader would hesitate (rule 2b).
5. **"San Francisco Bay Area"** (2 rows) is the region called "Bay Area" (rule 8), not the city: `US` and null.
6. **"Debar, Prishtina"** (1 row) sets a town in North Macedonia beside the capital of Kosovo in a "City, Region"
   pattern. Nothing settles which is meant, and Kosovo has no ISO 3166-1 code: null and null.
7. **Hong Kong** (2 rows) is both the place and its city: `HK` and "Hong Kong", as Singapore is `SG` and "Singapore".
8. **Three-letter country codes** ("IND", 6 rows) count as naming the country (rule 2a).
9. **Lists of bare cities** ("Atlanta; Austin; Baltimore; ...", "Hybrid - San Francisco, New York City, Austin"; 3 rows):
   the first city decides and rule 4 gives its country. The other cities are not used as evidence.
10. **Spelling.** Names keep their standard diacritics ("São Paulo", "Reykjavík"). Case and a trailing space do not
    matter ("dublin").
