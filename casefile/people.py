"""Work out who is who across every chunk, then give each person one short, unique label.

Naming rule (applied globally, after every file is read, so a label never changes
partway through the report):
  1. First name only:                          "Sarah"
  2. Two people share a first name -> add surname: "Sarah Kim", "Sarah Lee"
  3. Still identical (two John Smiths) -> number:  "John Smith #1", "John Smith #2"
  People known only by surname keep their title: "Dr. Patel".
  A bare first name that can't be pinned to one person is flagged: "Sarah (unclear)".
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

TITLES = {
    "mr", "mrs", "ms", "miss", "mx", "dr", "prof", "professor", "sir", "dame", "lord", "lady",
    "rev", "fr", "judge", "justice", "hon", "officer", "det", "detective", "sgt", "sergeant",
    "capt", "captain", "lt", "col", "gen", "agent", "insp", "inspector", "cllr", "councillor",
}
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "esq", "qc", "kc", "cpa"}
# Lower-case words that end a name: "Sarah at Northgate" is Sarah, not Sarah Northgate.
# Particles such as "van", "de", "bin" are not listed: they belong to real surnames.
DESCRIPTIVE_WORDS = {"at", "from", "of", "the", "and", "in", "with", "for", "to", "on", "who"}
ABBREVIATED_TITLES = {"mr", "mrs", "ms", "mx", "dr", "prof", "rev", "fr", "hon", "det", "sgt", "capt", "lt", "col", "gen", "insp", "cllr"}

# Nickname -> possible formal names. Only used when the surname also matches, and only
# when exactly one candidate exists, so "Chris Lee" is never forced onto the wrong Lee.
NICKNAMES: dict[str, set[str]] = {
    "bob": {"robert"}, "bobby": {"robert"}, "rob": {"robert"}, "robbie": {"robert"},
    "bill": {"william"}, "billy": {"william"}, "will": {"william"}, "liam": {"william"},
    "jim": {"james"}, "jimmy": {"james"}, "jamie": {"james"},
    "mike": {"michael"}, "mick": {"michael"}, "mikey": {"michael"},
    "dave": {"david"}, "tom": {"thomas"}, "tommy": {"thomas"}, "tony": {"anthony"},
    "dan": {"daniel"}, "danny": {"daniel"}, "matt": {"matthew"}, "nick": {"nicholas"},
    "steve": {"steven", "stephen"}, "joe": {"joseph"}, "jon": {"jonathan"}, "johnny": {"john", "jonathan"},
    "ben": {"benjamin"}, "ed": {"edward"}, "ted": {"edward"}, "eddie": {"edward"},
    "rick": {"richard"}, "rich": {"richard"}, "dick": {"richard"}, "charlie": {"charles"}, "chuck": {"charles"},
    "greg": {"gregory"}, "jeff": {"jeffrey"}, "ken": {"kenneth"}, "larry": {"lawrence"}, "pete": {"peter"},
    "andy": {"andrew"}, "drew": {"andrew"}, "tim": {"timothy"}, "phil": {"phillip", "philip"},
    "chris": {"christopher", "christina", "christine"}, "alex": {"alexander", "alexandra"},
    "sam": {"samuel", "samantha"}, "pat": {"patrick", "patricia"}, "jo": {"joanna", "josephine"},
    "liz": {"elizabeth"}, "beth": {"elizabeth"}, "betty": {"elizabeth"}, "lizzie": {"elizabeth"},
    "kate": {"katherine", "catherine", "kathryn"}, "katie": {"katherine", "catherine", "kathryn"},
    "kathy": {"katherine", "catherine", "kathryn"}, "cathy": {"catherine"},
    "jen": {"jennifer"}, "jenny": {"jennifer"}, "sue": {"susan"}, "suzy": {"susan"},
    "maggie": {"margaret"}, "meg": {"margaret"}, "peggy": {"margaret"}, "becky": {"rebecca"},
    "vicky": {"victoria"}, "debbie": {"deborah"}, "mandy": {"amanda"}, "abby": {"abigail"},
}


@dataclass(frozen=True)
class Name:
    first: str = ""
    last: str = ""
    title: str = ""

    @property
    def key(self) -> tuple:
        f, l = self.first.casefold(), self.last.casefold()
        if f and l:
            return ("I", f, l) if len(l) == 1 else ("F", f, l)
        return ("1", f) if f else ("L", l)


def parse_name(raw: str) -> Name | None:
    """'Dr. Sarah J. Kim', 'Kim, Sarah', 'Sarah' -> Name. Returns None for emails/junk."""
    raw = raw.strip()
    if not raw or "@" in raw:
        return None
    if raw.count(",") == 1:
        a, b = (x.strip() for x in raw.split(","))
        if a and b and " " not in a and b.lower().strip(". ") not in SUFFIXES:
            raw = f"{b} {a}"  # "Kim, Sarah" -> "Sarah Kim"
    tokens = re.split(r"[\s,]+", raw)
    # "Bob's agent", "the landlord's son": a description of someone, not their name.
    if any(re.search(r"['’]s$", t) for t in tokens):
        return None
    tokens = [t.strip(".'’-") for t in tokens]
    tokens = [t for t in tokens if t and any(ch.isalpha() for ch in t)]
    # Models sometimes return descriptions as names: "Sarah at Northgate", "Tom from the council".
    for i, tok in enumerate(tokens):
        if tok in DESCRIPTIVE_WORDS:
            tokens = tokens[:i]
            break
    title = ""
    while tokens and tokens[0].lower() in TITLES:
        tok = tokens.pop(0)
        if not title:
            title = tok.capitalize() + ("." if tok.lower() in ABBREVIATED_TITLES else "")
    while tokens and tokens[-1].lower() in SUFFIXES:
        tokens.pop()
    if not tokens:
        return None
    if len(tokens) == 1:
        # With a title, a lone word is a surname ("Dr. Patel"); without, a first name ("Sarah").
        return Name(last=tokens[0], title=title) if title else Name(first=tokens[0])
    return Name(first=tokens[0], last=tokens[-1], title=title)


def _first_compatible(a: str, b: str) -> bool:
    a, b = a.casefold(), b.casefold()
    return a == b or b in NICKNAMES.get(a, ()) or a in NICKNAMES.get(b, ())


@dataclass
class Person:
    id: int
    label: str = ""
    full_name: str = ""
    first: str = ""
    last: str = ""
    title: str = ""
    roles: Counter = field(default_factory=Counter)
    mentions: int = 0  # number of chunks that mention them
    first_seen: tuple = ()  # (doc_index, offset, "file, p. 3") - sortable
    events: int = 0
    first_event: str = ""  # date of their earliest / latest dated event
    last_event: str = ""
    unresolved: bool = False  # bare first name that matched several people

    def top_roles(self, n: int = 2) -> list[str]:
        """Most frequently stated roles, ignoring case repeats ("tenant" / "Tenant")."""
        roles: dict[str, str] = {}
        for role, _ in self.roles.most_common():
            roles.setdefault(role.casefold(), role)
        return list(roles.values())[:n]


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict = {}

    def add(self, x) -> None:
        self.parent.setdefault(x, x)

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


class Cast:
    """Collects name mentions chunk by chunk, then resolves them into people."""

    def __init__(self) -> None:
        self._uf = _UnionFind()
        self._raw_forms: dict[tuple, Counter] = defaultdict(Counter)  # key -> raw spellings
        self._titles: dict[tuple, Counter] = defaultdict(Counter)
        self._roles: dict[tuple, Counter] = defaultdict(Counter)
        self._chunks_seen: dict[tuple, set] = defaultdict(set)
        self._first_seen: dict[tuple, tuple] = {}
        # Per-chunk aliases: "Sarah" / "Mr. Kim" / an email only mean Sarah Kim *in that chunk*.
        self._local: dict[object, dict[str, tuple]] = defaultdict(dict)  # casefolded alias -> key
        self._local_forms: dict[object, set[str]] = defaultdict(set)  # aliases as written
        self._people: dict[tuple, Person] = {}  # root key -> Person, after finalize()
        self._relabel_cache: dict[object, tuple] = {}

    # -- collection --------------------------------------------------------------

    def add_person(self, chunk_id, full_name: str, aliases: list[str], role: str, seen: tuple) -> None:
        names = [(raw, parse_name(raw)) for raw in [full_name, *aliases]]
        parsed = [(raw, n) for raw, n in names if n]
        full = [(raw, n) for raw, n in parsed if n.key[0] == "F"]
        if not full:
            # "Sarah" + "Dr. Kim" in one record is enough to infer Sarah Kim.
            firsts = [n for _, n in parsed if n.key[0] == "1"]
            lasts = [n for _, n in parsed if n.key[0] == "L"]
            if len(firsts) == 1 and len(lasts) == 1:
                inferred = Name(firsts[0].first, lasts[0].last, lasts[0].title)
                full = [(f"{inferred.first} {inferred.last}", inferred)]
        if full:
            anchor = full[0][1].key
            for raw, n in full:
                self._register(n, raw, chunk_id, seen)
                self._uf.union(anchor, n.key)
            for raw, _ in names:
                self._add_local(chunk_id, raw, anchor)
            if role:
                self._roles[anchor][role] += 1
        elif parsed:
            anchor = parsed[0][1].key
            for raw, n in parsed:
                self._register(n, raw, chunk_id, seen)
                self._uf.union(anchor, n.key)
                self._add_local(chunk_id, raw, anchor)
            if role:
                self._roles[anchor][role] += 1

    def _add_local(self, chunk_id, raw: str, key: tuple) -> None:
        if raw.casefold() not in self._local[chunk_id]:
            self._local[chunk_id][raw.casefold()] = key
            self._local_forms[chunk_id].add(raw)

    def add_actor(self, chunk_id, name: str, seen: tuple) -> None:
        """Event actors the model forgot to list under people still count as people."""
        if name.casefold() not in self._local[chunk_id]:
            self.add_person(chunk_id, name, [], "", seen)

    def _register(self, n: Name, raw: str, chunk_id, seen: tuple) -> None:
        self._uf.add(n.key)
        self._raw_forms[n.key][raw] += 1
        if n.title:
            self._titles[n.key][n.title] += 1
        self._chunks_seen[n.key].add(chunk_id)
        if n.key not in self._first_seen or seen < self._first_seen[n.key]:
            self._first_seen[n.key] = seen

    # -- resolution --------------------------------------------------------------

    def finalize(self) -> list[Person]:
        keys = list(self._uf.parent)
        full_keys = [k for k in keys if k[0] == "F"]

        # Nicknames: "Bob Smith" joins "Robert Smith" if he is the only compatible Smith.
        by_last: dict[str, list[tuple]] = defaultdict(list)
        for k in full_keys:
            by_last[k[2]].append(k)
        for k in full_keys:
            if k[1] in NICKNAMES:
                cands = {self._uf.find(o) for o in by_last[k[2]] if o != k and _first_compatible(k[1], o[1])}
                if len(cands) == 1:
                    self._uf.union(cands.pop(), k)

        # Partial names join a full name only when exactly one person fits.
        roots = {self._uf.find(k) for k in full_keys}
        members: dict[tuple, list[tuple]] = defaultdict(list)
        for k in full_keys:
            members[self._uf.find(k)].append(k)
        ambiguous: set[tuple] = set()
        for k in keys:
            if k[0] == "F" or self._uf.find(k) in roots:
                continue
            if k[0] == "1":
                fits = {r for r in roots if any(_first_compatible(k[1], m[1]) for m in members[r])}
            elif k[0] == "L":
                fits = {r for r in roots if any(m[2] == k[1] for m in members[r])}
            else:  # "I": Sarah K.
                fits = {r for r in roots if any(m[1] == k[1] and m[2].startswith(k[2]) for m in members[r])}
            if len(fits) == 1:
                self._uf.union(fits.pop(), k)
            elif len(fits) > 1 and k[0] == "1":
                ambiguous.add(k)

        groups: dict[tuple, list[tuple]] = defaultdict(list)
        for k in keys:
            groups[self._uf.find(k)].append(k)
        people = []
        for root, group in groups.items():
            person = self._build_person(len(people), group)
            person.unresolved = any(k in ambiguous for k in group) and all(k[0] == "1" for k in group)
            self._people[root] = person
            people.append(person)
        _assign_labels(people)
        people.sort(key=lambda p: p.first_seen)
        return people

    def _build_person(self, pid: int, group: list[tuple]) -> Person:
        firsts: Counter = Counter()
        lasts: Counter = Counter()
        fulls: Counter = Counter()
        titles: Counter = Counter()
        for k in group:
            for raw, count in self._raw_forms[k].items():
                n = parse_name(raw)
                if n is None:
                    continue
                if n.first:
                    firsts[n.first] += count
                if n.last and len(n.last) > 1:
                    lasts[n.last] += count
                if n.first and n.last:
                    fulls[" ".join(raw.split())] += count
            titles.update(self._titles[k])
        roles: Counter = Counter()
        for k in group:
            roles.update(self._roles[k])
        p = Person(
            id=pid,
            first=_most_common(firsts),
            last=_most_common(lasts),
            title=_most_common(titles),
            roles=roles,
            mentions=len(set().union(*(self._chunks_seen[k] for k in group))),
            first_seen=min(self._first_seen[k] for k in group),
        )
        if p.first and p.last:
            # Prefer the longest commonly used full form, e.g. "Sarah J. Kim".
            p.full_name = max(fulls, key=lambda s: (fulls[s], len(s))) if fulls else f"{p.first} {p.last}"
        else:
            p.full_name = " ".join(x for x in (p.title, p.first or p.last) if x)
        return p

    # -- lookup after finalize ---------------------------------------------------

    def resolve(self, chunk_id, raw: str) -> Person | None:
        key = self._local.get(chunk_id, {}).get(raw.casefold())
        if key is None:
            n = parse_name(raw)
            if n is None or n.key not in self._uf.parent:
                return None
            key = n.key
        return self._people.get(self._uf.find(key))

    def relabel(self, chunk_id, text: str) -> str:
        """Rewrite names in a model-written sentence to each person's label.

        Case-sensitive on purpose: someone called "Will" must not turn "she will pay"
        into "she William pay"."""
        if chunk_id not in self._relabel_cache:
            variants = {}
            for raw in self._local_forms.get(chunk_id, ()):
                person = self.resolve(chunk_id, raw)
                if person:
                    variants[raw] = person.label
            pattern = None
            if variants:
                alternation = "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True))
                pattern = re.compile(rf"(?<![\w@])({alternation})(?![\w@])")
            self._relabel_cache[chunk_id] = (pattern, variants)
        pattern, variants = self._relabel_cache[chunk_id]
        return pattern.sub(lambda m: variants[m.group(1)], text) if pattern else text


def _most_common(counter: Counter) -> str:
    return max(counter, key=lambda s: (counter[s], len(s))) if counter else ""


def _assign_labels(people: list[Person]) -> None:
    def label(p: Person, level: int) -> str:
        if p.first and p.last:
            return p.first if level == 0 else f"{p.first} {p.last}"
        if p.first:
            return f"{p.first} (unclear)" if p.unresolved or level > 0 else p.first
        return f"{p.title} {p.last}" if p.title else p.last

    level = {p.id: 0 for p in people}
    for _ in range(2):
        buckets: dict[str, list[Person]] = defaultdict(list)
        for p in people:
            buckets[label(p, level[p.id]).casefold()].append(p)
        clashes = [ps for ps in buckets.values() if len(ps) > 1]
        if not clashes:
            break
        for ps in clashes:
            for p in ps:
                level[p.id] = 1
    for p in people:
        p.label = label(p, level[p.id])
    # Last resort for true duplicates (two different John Smiths): number by first appearance.
    buckets = defaultdict(list)
    for p in people:
        buckets[p.label.casefold()].append(p)
    for ps in buckets.values():
        if len(ps) > 1:
            for i, p in enumerate(sorted(ps, key=lambda p: p.first_seen), 1):
                p.label = f"{p.label} #{i}"
